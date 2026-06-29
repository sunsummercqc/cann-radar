#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stale_mr_notify.py — 超期 MR 扫描与邮件通知。

扫描 data/mrs/ 中所有 opened 状态的 MR，筛选出：
  1. 开启时间超过指定天数（默认 14 天）
  2. 包含指定 label（默认 ci-pipeline-passed）
按作者分组，通过邮件提醒其关闭或推进。

邮箱映射读取 config/gitcode_2_mail.txt（由 CI 从私仓注入）。
SMTP 配置复用 ~/.config/send-mail/config.ini 的 [smtp] 段。

用法：
    python stale_mr_notify.py --dry-run
    python stale_mr_notify.py
    python stale_mr_notify.py --stale-days 7 --label ci-pipeline-passed
    python stale_mr_notify.py --no-label
    python stale_mr_notify.py --report-to admin@huawei.com
"""

import argparse
import configparser
import json
import smtplib
import sys
from collections import defaultdict
from datetime import datetime
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

DEFAULT_STALE_DAYS = 14
DEFAULT_LABEL = "ci-pipeline-passed"


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


def scan_stale_mrs(stale_days, target_label, require_label, notify_paths=None):
    today = datetime.now()
    stale_by_author = defaultdict(list)
    stats = {"total_opened": 0, "stale_all": 0, "stale_matched": 0, "repos_scanned": 0}

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

            author = mr.get("author", "")
            if not author:
                continue

            stale_by_author[author].append({
                "repo": repo_path,
                "iid": mr.get("iid"),
                "title": mr.get("title", ""),
                "created_at": created_at,
                "days_open": days_open,
                "web_url": mr.get("web_url", ""),
                "labels": labels,
            })

    return stale_by_author, stats


def build_html_email(author, mrs):
    rows = ""
    for mr in sorted(mrs, key=lambda x: -x["days_open"]):
        labels_str = ", ".join(mr["labels"]) if mr["labels"] else "-"
        rows += f"""<tr>
  <td style="padding:8px 12px;border-bottom:1px solid #eee">{mr['repo']}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee">
    <a href="{mr['web_url']}" style="color:#2563eb;text-decoration:none">#{mr['iid']}</a>
  </td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{mr['title']}</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:center">{mr['days_open']}天</td>
  <td style="padding:8px 12px;border-bottom:1px solid #eee;font-size:12px;color:#666">{labels_str}</td>
</tr>"""

    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:720px;margin:0 auto">
  <h2 style="color:#1a1d2e;font-size:18px;margin-bottom:4px">超期 MR 提醒</h2>
  <p style="color:#666;font-size:13px;margin-bottom:16px">
    Hi {author}，您有 <strong style="color:#e05f5f">{len(mrs)}</strong> 个 MR 已开启超过 14 天且 CI 已通过，请及时处理。
  </p>
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


def send_one_email(cfg, to_email, subject, html_body):
    server = cfg.get("smtp", "server").strip()
    port = int(cfg.get("smtp", "port").strip())
    username = cfg.get("smtp", "username").strip()
    password = cfg.get("smtp", "password").strip()
    sender = cfg.get("mail", "from", fallback=username).strip()

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((str(Header("CANN Radar", "utf-8")), sender))
    msg["To"] = to_email
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL(server, port, timeout=30) as smtp:
        smtp.login(username, password)
        smtp.sendmail(sender, [to_email], msg.as_string())


def build_summary_html(stats, unmapped, no_email_users):
    rows = ""
    for author, count in sorted(unmapped.items(), key=lambda x: -x[1]):
        rows += f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>{author}</td><td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center'>{count}</td></tr>"

    no_email_section = ""
    if no_email_users:
        items = "".join(f"<li>{u} ({unmapped[u]}个MR)</li>" for u in sorted(no_email_users, key=lambda x: -unmapped[x]))
        no_email_section = f"""<h3 style="font-size:14px;margin-top:20px;color:#e05f5f">未找到邮箱的用户（{len(no_email_users)}人）</h3>
<ul style="font-size:13px;color:#333;line-height:1.8">{items}</ul>"""

    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto">
  <h2 style="color:#1a1d2e;font-size:18px">超期 MR 扫描报告</h2>
  <table style="font-size:13px;border-collapse:collapse;margin-bottom:16px">
    <tr><td style="padding:4px 16px 4px 0;color:#666">扫描仓库</td><td><strong>{stats['repos_scanned']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">Opened MR 总数</td><td><strong>{stats['total_opened']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">超期 MR（不限 label）</td><td><strong>{stats['stale_all']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">匹配超期 MR</td><td><strong>{stats['stale_matched']}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">涉及作者</td><td><strong>{len(unmapped)}</strong></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666">未映射邮箱</td><td><strong style="color:#e05f5f">{len(no_email_users)}</strong></td></tr>
  </table>
  {no_email_section}
  <p style="color:#999;font-size:11px;margin-top:16px">CANN Radar 自动生成</p>
</div>"""


def main():
    parser = argparse.ArgumentParser(description="超期 MR 扫描与邮件通知")
    parser.add_argument("--dry-run", action="store_true", help="仅打印结果，不发送邮件")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS, help=f"超期天数阈值（默认 {DEFAULT_STALE_DAYS}）")
    parser.add_argument("--label", default=DEFAULT_LABEL, help=f"需要匹配的 label（默认 {DEFAULT_LABEL}）")
    parser.add_argument("--no-label", action="store_true", help="不按 label 过滤，所有超期 MR 均纳入")
    parser.add_argument("--report-to", help="将汇总报告发送到此邮箱（含未映射用户清单）")
    parser.add_argument("--test", metavar="EMAIL", help="测试模式：发送 1 封样本邮件到指定邮箱，不发给实际作者")
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
    if args.test:
        print(f"  模式: 测试（所有邮件发送到 {args.test}）")
    elif args.dry_run:
        print(f"  模式: dry-run（不发送）")
    else:
        print(f"  模式: 正式发送")

    notify_paths = load_notify_repo_paths()
    if not notify_paths:
        print("  ✗ 无仓库配置 notify: true，请在 config/repos.yml 中设置")
        return 1
    print(f"  通知仓库: {', '.join(sorted(notify_paths))}")

    mail_map = load_mail_map()
    print(f"  邮箱映射: {len(mail_map)} 条")

    stale_by_author, stats = scan_stale_mrs(args.stale_days, target_label, require_label, notify_paths)
    print(f"\n  扫描结果:")
    print(f"    仓库: {stats['repos_scanned']}")
    print(f"    Opened MR: {stats['total_opened']}")
    print(f"    超期 MR（不限 label）: {stats['stale_all']}")
    print(f"    匹配超期 MR: {stats['stale_matched']}")
    print(f"    涉及作者: {len(stale_by_author)}")

    if not stale_by_author:
        print("\n  ✓ 无超期 MR，无需通知")
        return 0

    unmapped_count = {}
    no_email_users = []
    sent = 0
    failed = 0

    smtp_cfg = None
    if not args.dry_run:
        smtp_cfg = load_smtp_config()
        if not smtp_cfg:
            print("\n  ✗ SMTP 配置不可用，请使用 --dry-run 测试或先配置 SMTP")
            return 1

    print(f"\n=== 发送通知 ===")
    test_sent = False
    for author, mrs in sorted(stale_by_author.items(), key=lambda x: -len(x[1])):
        email = mail_map.get(author)
        unmapped_count[author] = len(mrs)

        if not email:
            no_email_users.append(author)
            print(f"  ⊘ {author}: {len(mrs)} 个超期 MR（无邮箱映射，跳过）")
            continue

        subject = f"[CANN] 您有 {len(mrs)} 个超期未关闭的 MR（{'CI已通过' if target_label else '需处理'}）"
        html = build_html_email(author, mrs)

        if args.dry_run:
            print(f"  → {author} <{email}>: {len(mrs)} 个 MR [dry-run，未发送]")
        elif args.test:
            if not test_sent:
                try:
                    send_one_email(smtp_cfg, args.test, subject, html)
                    sent += 1
                    test_sent = True
                    print(f"  ✓ {author} <{email}> → {args.test}: {len(mrs)} 个 MR [测试样本，仅此 1 封]")
                except Exception as e:
                    failed += 1
                    print(f"  ✗ {author} <{email}>: {e}")
            else:
                print(f"  ⊘ {author} <{email}>: {len(mrs)} 个 MR [测试模式，跳过]")
        else:
            try:
                send_one_email(smtp_cfg, email, subject, html)
                sent += 1
                print(f"  ✓ {author} <{email}>: {len(mrs)} 个 MR")
            except Exception as e:
                failed += 1
                print(f"  ✗ {author} <{email}>: {e}")

    print(f"\n=== 汇总 ===")
    print(f"  已发送: {sent}")
    print(f"  失败: {failed}")
    print(f"  无邮箱: {len(no_email_users)}")

    if args.report_to and not args.dry_run and smtp_cfg:
        print(f"\n=== 发送汇总报告 → {args.report_to} ===")
        summary_html = build_summary_html(stats, unmapped_count, no_email_users)
        try:
            send_one_email(
                smtp_cfg, args.report_to,
                f"[CANN] 超期 MR 扫描报告（{stats['stale_matched']} 个 MR，{len(no_email_users)} 人无邮箱）",
                summary_html,
            )
            print(f"  ✓ 汇总报告已发送")
        except Exception as e:
            print(f"  ✗ 汇总报告发送失败: {e}")
    elif args.report_to and args.dry_run:
        print(f"\n  → 汇总报告将发送到 {args.report_to} [dry-run，未发送]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
