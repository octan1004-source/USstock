# -*- coding: utf-8 -*-
"""
note.com 用 株式投資記事の下書きを自動生成し、メールで送るスクリプト。

流れ:
  1. yfinance で株価データを取得
  2. Claude API (web検索ツール有効) にニュース調査 + 記事執筆を依頼
  3. 生成された下書きをメールで送信

使い方:
  python generate_report.py --mode daily
  python generate_report.py --mode weekly

必要な環境変数 (GitHub Secrets 等で設定):
  ANTHROPIC_API_KEY
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO
"""

import os
import sys
import argparse
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
from datetime import datetime, timedelta

import yfinance as yf
import anthropic

from config import (
    US_INDICES,
    US_STOCKS,
    US_ETFS,
    JP_INDICES,
    JP_STOCKS,
    MUTUAL_FUNDS,
    PRIVATE_HOLDINGS,
    ALL_TICKERS,
)

CLAUDE_MODEL = "claude-sonnet-4-6"


def fetch_market_data(period="5d"):
    """各ティッカーの直近の価格変化を取得してテキストにまとめる"""
    lines = []
    for ticker, name in ALL_TICKERS.items():
        try:
            hist = yf.Ticker(ticker).history(period=period)
            if hist.empty or len(hist) < 2:
                lines.append(f"- {name}({ticker}): データ取得失敗")
                continue
            last_close = hist["Close"].iloc[-1]
            prev_close = hist["Close"].iloc[-2]
            change_pct = (last_close - prev_close) / prev_close * 100
            week_ago_close = hist["Close"].iloc[0]
            week_change_pct = (last_close - week_ago_close) / week_ago_close * 100
            lines.append(
                f"- {name}({ticker}): 終値 {last_close:,.2f} "
                f"(前日比 {change_pct:+.2f}%, {period}間 {week_change_pct:+.2f}%)"
            )
        except Exception as e:
            lines.append(f"- {name}({ticker}): 取得エラー ({e})")
    return "\n".join(lines)


def build_prompt(mode: str, market_data: str) -> str:
    today = datetime.now().strftime("%Y年%m月%d日")

    mutual_funds_text = "\n".join(f"- {f}" for f in MUTUAL_FUNDS)
    private_text = "\n".join(f"- {p}" for p in PRIVATE_HOLDINGS)

    if mode == "daily":
        length_instruction = (
            "文字数は800〜1200字程度。子どもを寝かしつけたあとの数分でサクッと読める"
            "「日次速報」のトーンで、テンポよく書いてください。"
        )
        scope_instruction = (
            "直近1営業日の値動きを中心に、保有銘柄の中から動きが大きかったものを2〜3個ピックアップして、"
            "簡潔に触れてください。"
        )
    else:  # weekly
        length_instruction = (
            "文字数は2000〜3000字程度。週末にじっくり書く「週次まとめ」のトーンで、"
            "セクション分けして詳しく書いてください。将来的にnoteの有料記事・メンバーシップ限定コンテンツ"
            "としても通用するような、読み応えのある内容を意識してください。"
        )
        scope_instruction = (
            "直近1週間の値動きの背景、主要な材料（決算・経済指標・地政学等）、"
            "来週以降の注目イベント、保有銘柄全体を振り返っての所感（ポートフォリオの偏りや今後の方針など）"
            "についても触れてください。"
        )

    prompt = f"""あなたはnote.comで株式投資について発信している「パパ投資家」のアシスタントです。
発信者は、子どもの寝かしつけが終わったあとの隙間時間に記事を書いている個人投資家です。
文体の参考として、初心者にもわかりやすく解説する「高校生でもわかる米国株（花子さん）」や、
テンポよく実体験を語る「とも米国株投資チャンネル」のような、親しみやすく実践的なトーンを意識してください。
ただし実在の発信者の文章や口調をそのまま模倣・引用するのではなく、あくまで雰囲気の参考程度にとどめてください。

以下の情報をもとに、{today}時点のnote記事の「下書き」を日本語で作成してください。

# 発信者のポートフォリオ（実際の保有銘柄）
## 値動きを追える銘柄・指数（yfinanceより取得）
{market_data}

## インデックス投資信託（基準価額は自動取得対象外、定性的に触れる）
{mutual_funds_text}

## その他（未上場株access商品など）
{private_text}

# 記事のペルソナ・方針
- 想定読者は「子育て中で忙しいが、米国株・グロース株に興味がある会社員パパ・ママ」。
- 気取らず、生活者目線の一言（「今日は寝かしつけに時間がかかった」等の軽い導入があってもよい）を交えつつ、
  中身はきちんとした情報整理にしてください。
- {scope_instruction}
- 各銘柄・指数の値動きについて、考えられる要因をweb検索で調べたニュースをもとに補足してください。
- {length_instruction}
- 投資助言ではなく、あくまで情報整理・所感の共有という立場で書いてください。断定的な投資判断は避けてください。
- 見出し（##）を使って読みやすく構成してください。
- 記事の最後に「本記事は情報提供を目的としており、投資判断は自己責任でお願いします」旨の一文を入れてください。
- 出力は記事本文のみ。前置きや説明文は不要です。タイトル案を1行目に「# タイトル」の形式で入れてください。
  タイトルには「パパ投資家」「寝かしつけ後」など、テーマが伝わるキーワードを入れてください。
"""
    return prompt


def generate_draft(mode: str, market_data: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("環境変数 ANTHROPIC_API_KEY が設定されていません。")

    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_prompt(mode, market_data)

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )

    parts = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def send_email(subject: str, body: str):
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    mail_to = os.environ.get("MAIL_TO")

    missing = [k for k, v in {
        "SMTP_HOST": smtp_host, "SMTP_USER": smtp_user,
        "SMTP_PASS": smtp_pass, "MAIL_TO": mail_to,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"メール送信に必要な環境変数が不足しています: {missing}")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = mail_to
    msg["Date"] = formatdate(localtime=True)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [mail_to], msg.as_string())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["daily", "weekly"], required=True)
    args = parser.parse_args()

    period = "5d" if args.mode == "daily" else "1mo"
    print(f"[{args.mode}] 株価データを取得中...")
    market_data = fetch_market_data(period=period)
    print(market_data)

    print(f"[{args.mode}] Claude APIで記事を生成中...")
    draft = generate_draft(args.mode, market_data)

    today_str = datetime.now().strftime("%Y-%m-%d")
    subject_prefix = "【日次】" if args.mode == "daily" else "【週次】"
    subject = f"{subject_prefix}note下書き 株式マーケットレポート {today_str}"

    print("メールを送信中...")
    send_email(subject, draft)
    print("完了しました。")


if __name__ == "__main__":
    main()
