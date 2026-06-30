#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stale_mr_notify.py — 超期 MR 扫描与邮件通知。

扫描 data/mrs/ 中所有 opened 状态的 MR，筛选出：
  1. 开启时间超过指定天数（默认 14 天）
  2. 包含指定 label（默认 ci-pipeline-passed）
  3. 距离上次通知 ≥ 7 天 或 尚未通知（通过 data/stale_mr_notified.json 去重）

升级机制：
  - 第 1 次通知：仅提醒开发者本人
  - 第 2 次通知（距上次 ≥7 天 MR 仍 open）：提醒开发者并抄送管理员
  - 此后永久跳过

按作者分为三类：
  - 内部开发者 + 有邮箱 → 直接发个人提醒邮件
  - 内部开发者 + 无邮箱 → 收入管理员汇总报告
  - 外部开发者           → 收入管理员汇总报告

管理员邮箱优先取 --report-to 参数，否则读取 config/admin_email.txt（CI 从私仓注入）。

用法：
    python stale_mr_notify.py --dry-run
    python stale_mr_notify.py
    python stale_mr_notify.py --stale-days 7 --label ci-pipeline-passed
    python stale_mr_notify.py --no-label
    python stale_mr_notify.py --report-to admin@huawei.com
    python stale_mr_notify.py --test someone@huawei.com
"""

import argparse
import configparser
import json
import smtplib
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MRS_DIR = DATA_DIR / "mrs"
REPOS_CONFIG_PATH = Path("config/repos.yml")
MAIL_MAP_PATH = Path("config/gitcode_2_mail.txt")
SMTP_CONFIG_PATH = Path("config/smtp_config.ini")
INTERNAL_DEVS_PATH = Path("config/internal_developers.txt")
ADMIN_EMAIL_PATH = Path("config/admin_email.txt")
NOTIFIED_PATH = DATA_DIR / "stale_mr_notified.json"

DEFAULT_STALE_DAYS = 14
DEFAULT_LABEL = "ci-pipeline-passed"
RESEND_INTERVAL_DAYS = 7
MAX_NOTIFY_COUNT = 2


def load_notify_repo_paths():
    if not REPOS_CONFIG_PATH.exists():
        print(f"  ✗ 仓库配置不存在: {REPOS_CONFIG_PATH}")
        return set()
    with open(REPOS_CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    repos = config.get("repos", []) or []
    paths = set()
    for repo in repos:
        if repo.get("enabled", True) and repo.get("notify", False):
            paths.add(repo["path"])
    return paths


def load_mail_map():
    mapping = {}
    if not MAIL_MAP_PATH.exists():
        print(f"  ✗ 邮箱映射文件不存在: {MAIL_MAP_PATH}")
        return mapping
    for line in MAIL_MAP_PATH.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        username, col2, col3 = parts[0], parts[1], parts[2]
        if not username:
            continue
        email = col2 if col2 and col2 != "null" else col3
        if email and email != "null":
            mapping[username] = email
    return mapping


def load_internal_developers():
    devs = set()
    if not INTERNAL_DEVS_PATH.exists():
        print(f"  ✗ 内部开发者名单不存在: {INTERNAL_DEVS_PATH}")
        return devs
    for line in INTERNAL_DEVS_PATH.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if name and not name.startswith("#"):
            devs.add(name)
    return devs


def load_admin_email():
    if not ADMIN_EMAIL_PATH.exists():
        return None
    text = ADMIN_EMAIL_PATH.read_text(encoding="utf-8").strip()
    if text:
        return text.splitlines()[0].strip()
    return None


def load_notified():
    if not NOTIFIED_PATH.exists():
        return {"last_run": None, "notified": {}}
    try:
        return json.loads(NOTIFIED_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return {"last_run": None, "notified": {}}


def save_notified(notified):
    notified["last_run"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    NOTIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTIFIED_PATH.write_text(json.dumps(notified, ensure_ascii=False, indent=2), encoding="utf-8")


def _mr_key(repo_path, iid):
    safe = repo_path.replace("/", "__")
    return f"{safe}!{iid}"


SMTP_CONFIG_TEMPLATE = """\
[smtp]
server = smtp.huawei.com
port = 465
username = your_name@huawei.com
password = your_auth_code

[mail]
from = your_name@huawei.com
"""


def init_smtp_config():
    if SMTP_CONFIG_PATH.exists():
        print(f"  SMTP 配置已存在: {SMTP_CONFIG_PATH}")
        return
    SMTP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SMTP_CONFIG_PATH.write_text(SMTP_CONFIG_TEMPLATE, encoding="utf-8")
    print(f"  ✓ 已生成 SMTP 配置模板: {SMTP_CONFIG_PATH}")
    print(f"    请编辑该文件，填入实际的邮箱地址和授权码。")


def load_smtp_config():
    if not SMTP_CONFIG_PATH.exists():
        print(f"  ✗ SMTP 配置不存在: {SMTP_CONFIG_PATH}")
        print(f"    请先运行: python stale_mr_notify.py --init-smtp")
        return None
    cfg = configparser.ConfigParser()
    cfg.read(SMTP_CONFIG_PATH, encoding="utf-8")
    required_keys = ["server", "port", "username", "password"]
    for key in required_keys:
        if not cfg.get("smtp", key, fallback="").strip():
            print(f"  ✗ SMTP 配置缺少 [smtp] {key}")
            return None
    return cfg


def _check_mr_notify_status(key, notified, today):
    """返回 (should_notify, notify_stage)。
    notify_stage: 0=跳过, 1=首次通知, 2=二次升级通知
    """
    if key not in notified:
        return True, 1
    record = notified[key]
    count = record.get("count", 1)
    if count >= MAX_NOTIFY_COUNT:
        return False, 0
    last_at = record.get("notified_at", "")
    if not last_at:
        return False, 0
    try:
        last_dt = datetime.strptime(last_at[:10], "%Y-%m-%d")
    except ValueError:
        return False, 0
    if (today - last_dt).days >= RESEND_INTERVAL_DAYS:
        return True, count + 1
    return False, 0


def scan_stale_mrs(stale_days, target_label, require_label, notify_paths=None, notified=None):
    today = datetime.now()
    stale_by_author = defaultdict(list)
    stats = {
        "total_opened": 0, "stale_all": 0, "stale_matched": 0,
        "repos_scanned": 0, "skipped_recent": 0, "skipped_done": 0,
        "stage1_count": 0, "stage2_count": 0,
    }

    if notified is None:
        notified = {}

    if not MRS_DIR.exists():
        print(f"  ✗ MR 数据目录不存在: {MRS_DIR}")
        return stale_by_author, stats

    for f in sorted(MRS_DIR.glob("*.json")):
        repo_path = f.stem.replace("__", "/", 1)
        if notify_paths is not None and repo_path not in notify_paths:
            continue
        mrs = json.loads(f.read_text(encoding="utf-8"))
        stats["repos_scanned"] += 1

        for mr in mrs:
            if mr.get("state") != "opened":
                continue
            stats["total_opened"] += 1

            iid = mr.get("iid")
            if iid is None:
                continue
            key = _mr_key(repo_path, iid)

            should_notify, stage = _check_mr_notify_status(key, notified, today)
            if not should_notify:
                if key in notified:
                    stats["skipped_done"] += 1
                else:
                    stats["skipped_recent"] += 1
                continue

            created_at = mr.get("created_at", "")
            if not created_at:
                continue
            try:
                created = datetime.strptime(created_at[:10], "%Y-%m-%d")
            except ValueError:
                continue
            days_open = (today - created).days
            if days_open <= stale_days:
                continue
            stats["stale_all"] += 1

            labels = mr.get("labels") or []
            if require_label and target_label and target_label not in labels:
                continue
            stats["stale_matched"] += 1

            if stage == 2:
                stats["stage2_count"] += 1
            else:
                stats["stage1_count"] += 1

            author = mr.get("author", "")
            if not author:
                continue

            stale_by_author[author].append({
                "repo": repo_path,
                "iid": iid,
                "title": mr.get("title", ""),
                "created_at": created_at,
                "days_open": days_open,
                "web_url": mr.get("web_url", ""),
                "labels": labels,
                "notify_stage": stage,
            })

    return stale_by_author, stats


def _build_mr_table_rows(mrs):
    rows = ""
    for mr in sorted(mrs, key=lambda x: -x["days_open"]):
        labels_str = ", ".join(mr["labels"]) if mr["labels"] else "-"
        stage_note = " <span style='color:#e05f5f;font-size:11px'>(二次提醒)</span>" if mr.get("notify_stage") == 2 else ""
        rows += f"""<tr>
  <td style="padding:8px 12px;border-bottom:1px solid #eee">{mr['repo']}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee">
    <a href="{mr['web_url']}" style="color:#2563eb;text-decoration:none">#{mr['iid']}</a>
  </td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{mr['title']}{stage_note}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center">{mr['days_open']}天</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;color:#666">{labels_str}</td>
</tr>"""
    return rows


def build_html_email(author, mrs, is_escalated=False):
    stage2_mrs = [m for m in mrs if m.get("notify_stage") == 2]
    rows = _build_mr_table_rows(mrs)
    escalation_note = ""
    if stage2_mrs:
        escalation_note = f"""
  <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:12px 16px;margin-bottom:16px">
    <strong style="color:#856404">⚠ 以下 {len(stage2_mrs)} 个 MR 已二次提醒，并抄送管理员跟进。</strong>
  </div>"""
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:0 auto">
  <h2 style="color:#1a1d2e;font-size:18px;margin-bottom:4px">超期 MR 提醒</h2>
  <p style="color:#666;font-size:13px;margin-bottom:16px">
    Hi {author}，您有 <strong style="color:#e05f5f">{len(mrs)}</strong> 个 MR 已开启超过 14 天且 CI 已通过，请及时处理。
  </p>
  {escalation_note}
  <table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea;border-radius:8px;overflow:hidden">
    <thead>
      <tr style="background:#f0f2f5">
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">仓库</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">MR</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">标题</th>
        <th style="padding:10px 12px;text-align:center;font-weight:600;color:#1a1d2e">开启时长</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Labels</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="color:#999;font-size:11px;margin-top:16px">
    此邮件由 CANN Radar 自动发送，请检查 MR 状态后及时合并或关闭。
  </p>
</div>"""


def _build_author_mr_section(title, label_color, author_mrs_dict):
    if not author_mrs_dict:
        return ""
    total_mrs = sum(len(mrs) for mrs in author_mrs_dict.values())
    author_blocks = ""
    for author in sorted(author_mrs_dict.keys()):
        mrs = author_mrs_dict[author]
        rows = _build_mr_table_rows(mrs)
        author_blocks += f"""
  <h4 style="font-size:14px;margin:20px 0 8px;color:#1a1d2e">{author}（{len(mrs)} 个 MR）</h4>
  <table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e2e4ea;border-radius:8px;overflow:hidden;margin-bottom:16px">
    <thead>
      <tr style="background:#f0f2f5">
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">仓库</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">MR</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">标题</th>
        <th style="padding:10px 12px;text-align:center;font-weight:600;color:#1a1d2e">开启时长</th>
        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#1a1d2e">Labels</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>"""

    return f"""
  <h3 style="font-size:15px;margin-top:24px;color:{label_color};border-bottom:1px solid #e2e4ea;padding-bottom:6px">
    {title}（{len(author_mrs_dict)} 人，{total_mrs} 个 MR）
  </h3>{author_blocks}"""


def build_admin_report_html(stats, internal_no_email, external_authors, stale_days, target_label):
    internal_section = _build_author_mr_section(
        "内部开发者（无邮箱映射）", "#e05f5f", internal_no_email,
    )
    external_section = _build_author_mr_section(
        "外部开发者", "#f5a623", external_authors,
    )

    total_internal = sum(len(mrs) for mrs in internal_no_email.values())
    total_external = sum(len(mrs) for mrs in external_authors.values())
    stage2_total = stats.get("stage2_count", 0)

    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:0 auto">
  <h2 style="color:#1a1d2e;font-size:18px;margin-bottom:4px">超期 MR 管理员汇总报告</h2>
  <p style="color:#666;font-size:13px;margin-bottom:16px">
    扫描条件：开启超过 <strong>{stale_days}</strong> 天{'，label=' + target_label if target_label else ''}（距上次通知≥{RESEND_INTERVAL_DAYS}天可重发，最多{MAX_NOTIFY_COUNT}次）
  </p>
  <table style="font-size:13px;border-collapse:collapse;margin-bottom:20px">
    <tr><td style="padding:4px 16px 4px 0;color:#666">扫描仓库</td><td><strong>{stats['repos_scanned']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">Opened MR 总数</td><td><strong>{stats['total_opened']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">超期 MR（不限 label）</td><td><strong>{stats['stale_all']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">匹配超期 MR</td><td><strong>{stats['stale_matched']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">首次通知</td><td><strong>{stats.get('stage1_count', 0)}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">二次升级通知</td><td><strong style="color:#e05f5f">{stage2_total}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">近{RESEND_INTERVAL_DAYS}天已通知跳过</td><td><strong>{stats['skipped_recent']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">已达上限永久跳过</td><td><strong>{stats['skipped_done']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">内部无邮箱 MR 数</td><td><strong style="color:#e05f5f">{total_internal}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">外部开发者 MR 数</td><td><strong style="color:#f5a623">{total_external}</strong></td></tr>
  </table>
  {internal_section}
  {external_section}
  <p style="color:#999;font-size:11px;margin-top:20px">CANN Radar 自动生成 · 内部无邮箱开发者及外部开发者需管理员介入联系</p>
</div>"""


def send_one_email(cfg, to_email, subject, html_body, cc_email=None):
    server = cfg.get("smtp", "server").strip()
    port = int(cfg.get("smtp", "port").strip())
    username = cfg.get("smtp", "username").strip()
    password = cfg.get("smtp", "password").strip()
    sender = cfg.get("mail", "from", fallback=username).strip()

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((str(Header("CANN Radar", "utf-8")), sender))
    msg["To"] = to_email
    if cc_email:
        msg["Cc"] = cc_email
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    recipients = [to_email]
    if cc_email:
        recipients.append(cc_email)

    with smtplib.SMTP_SSL(server, port, timeout=30) as smtp:
        smtp.login(username, password)
        smtp.sendmail(sender, recipients, msg.as_string())


def _send_personal_emails(internal_with_email, target_label, smtp_cfg, notified_data, admin_email, args):
    sent = 0
    failed = 0
    test_sent = False

    for author, (email, mrs) in sorted(internal_with_email.items(), key=lambda x: -len(x[1][1])):
        has_stage2 = any(m.get("notify_stage") == 2 for m in mrs)
        stage2_count = sum(1 for m in mrs if m.get("notify_stage") == 2)
        subject = f"[CANN] 您有 {len(mrs)} 个超期未关闭的 MR（{'CI已通过' if target_label else '需处理'}）"
        if has_stage2:
            subject += " [二次提醒]"
        html = build_html_email(author, mrs, is_escalated=has_stage2)

        cc = None
        if has_stage2 and admin_email and admin_email != email:
            cc = admin_email

        if args.dry_run:
            cc_str = f" 抄送:{cc}" if cc else ""
            stage_note = f" 其中{stage2_count}个二次提醒" if has_stage2 else ""
            print(f"  → {author} <{email}>{cc_str}: {len(mrs)} 个 MR{stage_note} [dry-run，未发送]")
        elif args.test:
            if not test_sent:
                try:
                    send_one_email(smtp_cfg, args.test, subject, html, cc_email=cc)
                    sent += 1
                    test_sent = True
                    stage_note = f" 含{stage2_count}个二次提醒" if has_stage2 else ""
                    print(f"  ✓ {author} <{email}> → {args.test}: {len(mrs)} 个 MR{stage_note} [测试样本，仅此1封]")
                    _mark_notified(notified_data, mrs)
                except Exception as e:
                    failed += 1
                    print(f"  ✗ {author} <{email}>: {e}")
            else:
                print(f"  ⊘ {author} <{email}>: {len(mrs)} 个 MR [测试模式，跳过]")
        else:
            try:
                send_one_email(smtp_cfg, email, subject, html, cc_email=cc)
                sent += 1
                _mark_notified(notified_data, mrs)
                cc_str = f"，抄送管理员" if cc else ""
                stage_note = f" 含{stage2_count}个二次提醒" if has_stage2 else ""
                print(f"  ✓ {author} <{email}>: {len(mrs)} 个 MR{stage_note}{cc_str}")
            except Exception as e:
                failed += 1
                print(f"  ✗ {author} <{email}>: {e}")

    return sent, failed


def _mark_notified(notified_data, mrs):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for mr in mrs:
        key = _mr_key(mr["repo"], mr["iid"])
        if not key:
            continue
        existing = notified_data["notified"].get(key)
        new_count = (existing.get("count", 0) + 1) if existing else 1
        notified_data["notified"][key] = {"notified_at": now, "count": new_count}


def main():
    parser = argparse.ArgumentParser(description="超期 MR 扫描与邮件通知")
    parser.add_argument("--dry-run", action="store_true", help="仅打印结果，不发送邮件")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS, help=f"超期天数阈值（默认 {DEFAULT_STALE_DAYS}）")
    parser.add_argument("--label", default=DEFAULT_LABEL, help=f"需要匹配的 label（默认 {DEFAULT_LABEL}）")
    parser.add_argument("--no-label", action="store_true", help="不按 label 过滤，所有超期 MR 均纳入")
    parser.add_argument("--report-to", help="管理员汇总报告发送到此邮箱（覆盖 config/admin_email.txt）")
    parser.add_argument("--test", metavar="EMAIL", help="测试模式：仅发送1封样本到指定邮箱，不发给实际作者")
    parser.add_argument("--init-smtp", action="store_true", help="生成 SMTP 配置模板到 config/smtp_config.ini")
    args = parser.parse_args()

    if args.init_smtp:
        init_smtp_config()
        return 0

    require_label = not args.no_label
    target_label = "" if args.no_label else args.label

    print(f"=== 超期 MR 扫描 ===")
    print(f"  超期天数: >{args.stale_days}天")
    print(f"  Label 过滤: {'无' if args.no_label else target_label}")
    print(f"  升级间隔: 距上次通知≥{RESEND_INTERVAL_DAYS}天可重发（最多{MAX_NOTIFY_COUNT}次）")
    if args.test:
        print(f"  模式: 测试（仅1封样本发送到 {args.test}）")
    elif args.dry_run:
        print(f"  模式: dry-run（不发送）")
    else:
        print(f"  模式: 正式发送")

    internal_devs = load_internal_developers()
    print(f"  内部开发者: {len(internal_devs)} 人")

    notify_paths = load_notify_repo_paths()
    if not notify_paths:
        print("  ✗ 无仓库配置 notify: true，请在 config/repos.yml 中设置")
        return 1
    print(f"  通知仓库: {', '.join(sorted(notify_paths))}")

    mail_map = load_mail_map()
    print(f"  邮箱映射: {len(mail_map)} 条")

    notified_data = load_notified()
    print(f"  已追踪 MR: {len(notified_data.get('notified', {}))} 个")

    admin_email = args.report_to or load_admin_email()
    if admin_email:
        print(f"  管理员邮箱: {admin_email}")
    else:
        print(f"  管理员邮箱: 未配置（将不发送汇总报告）")

    stale_by_author, stats = scan_stale_mrs(
        args.stale_days, target_label, require_label, notify_paths, notified_data.get("notified", {}),
    )
    print(f"\n  扫描结果:")
    print(f"    仓库: {stats['repos_scanned']}")
    print(f"    Opened MR: {stats['total_opened']}")
    print(f"    超期 MR（不限 label）: {stats['stale_all']}")
    print(f"    匹配超期 MR: {stats['stale_matched']}")
    print(f"    首次通知: {stats['stage1_count']}")
    print(f"    二次升级: {stats['stage2_count']}")
    print(f"    近{RESEND_INTERVAL_DAYS}天已通知跳过: {stats['skipped_recent']}")
    print(f"    已达上限跳过: {stats['skipped_done']}")
    print(f"    涉及作者: {len(stale_by_author)}")

    if not stale_by_author:
        print("\n  ✓ 无新增/待升级超期 MR，无需通知")
        return 0

    # 分类：内部有邮箱 / 内部无邮箱 / 外部
    internal_with_email = {}
    internal_no_email = {}
    external_authors = {}

    for author, mrs in stale_by_author.items():
        if author in internal_devs:
            email = mail_map.get(author)
            if email:
                internal_with_email[author] = (email, mrs)
            else:
                internal_no_email[author] = mrs
        else:
            external_authors[author] = mrs

    print(f"\n  分类结果:")
    print(f"    内部+有邮箱: {len(internal_with_email)} 人（{sum(len(v[1]) for v in internal_with_email.values())} 个 MR）")
    print(f"    内部+无邮箱: {len(internal_no_email)} 人（{sum(len(v) for v in internal_no_email.values())} 个 MR）")
    print(f"    外部开发者: {len(external_authors)} 人（{sum(len(v) for v in external_authors.values())} 个 MR）")

    smtp_cfg = None
    if not args.dry_run:
        smtp_cfg = load_smtp_config()
        if not smtp_cfg:
            print("\n  ✗ SMTP 配置不可用，请使用 --dry-run 测试或先配置 SMTP")
            return 1

    notified_changed = False

    # 发送个人通知（仅内部有邮箱者）
    print(f"\n=== 发送个人通知 ===")
    if not internal_with_email:
        print("  （无内部+有邮箱的开发者，跳过个人通知）")
    else:
        sent, failed = _send_personal_emails(
            internal_with_email, target_label, smtp_cfg, notified_data, admin_email, args,
        )
        if sent > 0 and not args.dry_run:
            notified_changed = True
        if not args.dry_run:
            print(f"\n  个人通知: 已发送 {sent}, 失败 {failed}")

    # 发送管理员汇总报告（内部无邮箱 + 外部开发者）
    if args.dry_run:
        if admin_email and (internal_no_email or external_authors):
            print(f"\n=== 管理员汇总报告 ===")
            print(f"  → 将发送到 {admin_email} [dry-run，未发送]")
            print(f"    内部无邮箱: {len(internal_no_email)} 人")
            print(f"    外部开发者: {len(external_authors)} 人")
    elif admin_email and (internal_no_email or external_authors):
        print(f"\n=== 发送管理员汇总报告 → {admin_email} ===")
        admin_html = build_admin_report_html(
            stats, internal_no_email, external_authors, args.stale_days, target_label,
        )
        try:
            send_one_email(
                smtp_cfg, admin_email,
                f"[CANN] 超期 MR 管理员报告（内部无邮箱 {len(internal_no_email)} 人，外部 {len(external_authors)} 人）",
                admin_html,
            )
            print(f"  ✓ 管理员汇总报告已发送")
        except Exception as e:
            print(f"  ✗ 管理员汇总报告发送失败: {e}")

    # 保存 tracking 文件
    if notified_changed and not args.dry_run:
        save_notified(notified_data)
        print(f"\n  ✓ 已更新通知记录: {NOTIFIED_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
