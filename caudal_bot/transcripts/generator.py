"""Transcript rendering: the plain-text .txt and the standalone Discord-styled .html
(markdown, mentions, embeds, attachments, sanitisation, dark-theme CSS). Pure
functions: no Discord API calls, no file I/O."""

from __future__ import annotations

import datetime as dt
import html
import io
import re
from dataclasses import dataclass, field

import discord

from ..common import format_duration


TRANSCRIPT_HISTORY_LIMIT = 500
# Deletion never waits on a slow transcript longer than this.
TRANSCRIPT_TIMEOUT_SECONDS = 20
_TS_FORMAT = "%Y-%m-%d %H:%M"


def _user_label(user: discord.abc.User | None, fallback_id: int | None = None) -> str:
    if user is None:
        return f"Unknown user ({fallback_id})" if fallback_id else "—"
    return f"{user.name} ({user.id})"


def _format_transcript_message(msg: discord.Message) -> str:
    parts: list[str] = []
    if msg.clean_content:
        parts.append(msg.clean_content)
    for embed in msg.embeds:
        body = " — ".join(p for p in (embed.title, embed.description) if p)
        parts.append(f"[Embed] {body}".rstrip())
        parts.extend(f"{f.name}: {f.value}" for f in embed.fields)
    parts.extend(f"[Attachment] {a.filename} <{a.url}>" for a in msg.attachments)
    parts.extend(f"[Sticker] {s.name}" for s in msg.stickers)
    text = "\n".join(parts) or "[no text content]"
    # Indent continuation lines so each entry stays visually one block.
    text = text.replace("\n", "\n    ")
    return f"[{msg.created_at.strftime(_TS_FORMAT)}] {msg.author.name}: {text}"


def build_transcript(
    *,
    channel_name: str,
    opener: str,
    claimed_staff: str,
    closed_by: str,
    reason: str,
    opened_at: dt.datetime,
    closed_at: dt.datetime,
    messages: list[discord.Message],
    truncated: bool,
    deleted_by: str | None = None,
    deleted_at: dt.datetime | None = None,
    response_time: str = "—",
) -> str:
    """Plain-text transcript. Leave deleted_* unset for a ticket that is archived but not yet deleted."""
    count = f"{len(messages)}" + (f" (only the first {len(messages)} were captured)" if truncated else "")
    header = [
        "=" * 60,
        f"Ticket Transcript — #{channel_name}",
        "=" * 60,
        f"Opener        : {opener}",
        f"Claimed Staff : {claimed_staff}",
        f"Closed By     : {closed_by}",
        f"Close Reason  : {reason}",
        f"Deleted By    : {deleted_by or '— (archived, not yet deleted)'}",
        f"Opened        : {opened_at.strftime(_TS_FORMAT)} UTC",
        f"Closed        : {closed_at.strftime(_TS_FORMAT)} UTC",
        f"Deleted       : {deleted_at.strftime(_TS_FORMAT) + ' UTC' if deleted_at else '—'}",
        f"Response Time : {response_time}",
        f"Resolution    : {format_duration(closed_at - opened_at)} (opened → closed)",
        f"Messages      : {count}",
        "=" * 60,
        "",
    ]
    return "\n".join(header + [_format_transcript_message(m) for m in messages]) + "\n"


@dataclass(frozen=True)
class TranscriptData:
    text: str
    owner: discord.abc.User | None
    owner_id: int | None
    staff_id: int | None
    closer_id: int | None
    reason: str
    duration: str  # total resolution time: opened -> closed
    message_count: int
    response_time: str = "No staff response"
    handled_by_id: int | None = None
    opened_at: dt.datetime | None = None
    closed_at: dt.datetime | None = None
    first_response_at: dt.datetime | None = None
    html: str = ""

    # Fresh File objects each call: discord.File is consumed when sent, so it can't be reused.
    def as_file(self, channel_name: str) -> discord.File:
        return discord.File(io.BytesIO(self.text.encode("utf-8")), filename=f"transcript-{channel_name}.txt")


# Everything user-controlled goes through html.escape before any markup is added,
# and the files are also served with a no-script CSP (see Dashboard), so a crafted
# message can't run script in whoever opens the transcript.

# Discord's default-avatar palette, used for the placeholder circle behind each avatar.
_AVATAR_COLORS = ("#5865f2", "#757e8a", "#3ba55c", "#faa61a", "#ed4245", "#eb459e")
_GROUP_WINDOW = dt.timedelta(minutes=7)  # Discord merges same-author messages sent within this

_TRANSCRIPT_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#313338;color:#dbdee1;font:16px/1.375 "gg sans","Noto Sans","Helvetica Neue",Helvetica,Arial,sans-serif}
a{color:#00a8fc;text-decoration:none}a:hover{text-decoration:underline}
.header{background:#2b2d31;padding:24px 32px;border-bottom:1px solid #1f2023}
.server{color:#b5bac1;font-size:14px;font-weight:600;text-transform:uppercase;letter-spacing:.02em}
.header h1{margin:4px 0 0;font-size:24px;color:#f2f3f5}
.meta{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin:18px 0 0;padding:0}
.meta div{background:#1e1f22;border-radius:8px;padding:10px 12px}
.meta dt{font-size:12px;font-weight:700;text-transform:uppercase;color:#949ba4;letter-spacing:.02em}
.meta dd{margin:3px 0 0;color:#f2f3f5;overflow-wrap:anywhere}
.messages{padding:8px 0 32px}
.msg{display:flex;padding:2px 32px 2px 16px;position:relative}
.msg:hover{background:#2e3035}
.msg.first{margin-top:17px}
.gutter{width:56px;flex:none}
.avatar{width:40px;height:40px;border-radius:50%;position:relative;overflow:hidden;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:600;font-size:18px;margin-top:2px}
.avatar img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.hover-time{visibility:hidden;font-size:11px;color:#949ba4;display:block;text-align:right;padding-right:12px;line-height:22px}
.msg:hover .hover-time{visibility:visible}
.body{min-width:0;flex:1}
.name{color:#f2f3f5;font-weight:500}
.badge{display:inline-block;background:#5865f2;color:#fff;font-size:10px;font-weight:600;line-height:15px;padding:0 4px;border-radius:3px;margin-left:4px;vertical-align:1px}
.ts{color:#949ba4;font-size:12px;margin-left:6px}
.content{overflow-wrap:anywhere}
code{background:#2b2d31;border-radius:4px;padding:0 .2em;font:85%/1.4 Consolas,"Andale Mono WT",Monaco,monospace}
pre{background:#2b2d31;border:1px solid #1e1f22;border-radius:4px;padding:8px;margin:4px 0;overflow-x:auto;max-width:90%;white-space:pre-wrap}
pre code{padding:0;background:none}
blockquote{margin:2px 0;padding:0 0 0 12px;border-left:4px solid #4e5058}
h3,h4,h5{margin:8px 0 2px;color:#f2f3f5}
.mention{background:rgba(88,101,242,.3);color:#c9cdfb;border-radius:3px;padding:0 2px;font-weight:500}
.spoiler{background:#1e1f22;color:transparent;border-radius:3px}.spoiler:hover{color:inherit}
.subtext{font-size:12px;color:#949ba4}
.embed{background:#2b2d31;border-left:4px solid #1e1f22;border-radius:4px;max-width:520px;padding:8px 16px 14px 12px;margin-top:4px}
.embed-author{font-size:14px;font-weight:600;color:#f2f3f5;margin-top:8px}
.embed-title{font-weight:600;color:#f2f3f5;margin-top:8px}
.embed-desc{font-size:14px;margin-top:8px}
.fields{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 16px;margin-top:8px}
.field{grid-column:1/-1}.field.inline{grid-column:auto}
.field-name{font-size:14px;font-weight:600;color:#f2f3f5}
.field-value{font-size:14px}
.embed-footer{font-size:12px;color:#b5bac1;margin-top:8px}
.att-img{display:block;max-width:min(400px,100%);max-height:300px;border-radius:8px;margin-top:4px}
.attachment{display:flex;gap:10px;align-items:center;background:#2b2d31;border:1px solid #1e1f22;border-radius:8px;padding:10px 12px;margin-top:4px;max-width:420px}
.attachment .size{color:#949ba4;font-size:12px}
.foot{color:#949ba4;font-size:12px;text-align:center;padding:16px;border-top:1px solid #3f4147}
@media (max-width:640px){.header{padding:18px 16px}.msg{padding-right:12px;padding-left:8px}.gutter{width:48px}.fields{grid-template-columns:1fr}}
"""


@dataclass
class _Resolver:
    """Names for raw <@id>, <@&id> and <#id> tokens, which embeds carry unresolved."""
    users: dict[int, str] = field(default_factory=dict)
    roles: dict[int, str] = field(default_factory=dict)
    channels: dict[int, str] = field(default_factory=dict)


_TS_TOKEN_FORMATS = {"t": "%H:%M", "T": "%H:%M:%S", "d": "%d/%m/%Y", "D": "%d %B %Y", "f": "%d %B %Y %H:%M",
                     "F": "%A, %d %B %Y %H:%M", "R": "%d %B %Y %H:%M"}


def _render_tokens(escaped: str, r: _Resolver, wrap=lambda fragment: fragment) -> str:
    """Replace Discord's (already HTML-escaped) mention / timestamp / emoji tokens.

    ``wrap`` lets the caller shield the output from later markdown passes, so a
    name like "some_user_name" isn't partly italicised.
    """
    def user(m: re.Match) -> str:
        return wrap(f'<span class="mention">@{html.escape(r.users.get(int(m[1]), "unknown-user"))}</span>')

    def role(m: re.Match) -> str:
        return wrap(f'<span class="mention">@{html.escape(r.roles.get(int(m[1]), "unknown-role"))}</span>')

    def chan(m: re.Match) -> str:
        return wrap(f'<span class="mention">#{html.escape(r.channels.get(int(m[1]), "unknown-channel"))}</span>')

    def stamp(m: re.Match) -> str:
        try:
            when = dt.datetime.fromtimestamp(int(m[1]), dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return m[0]
        return wrap(f"<code>{when.strftime(_TS_TOKEN_FORMATS.get(m[2] or 'f', '%d %B %Y %H:%M'))} UTC</code>")

    escaped = re.sub(r"&lt;@&amp;(\d+)&gt;", role, escaped)
    escaped = re.sub(r"&lt;@!?(\d+)&gt;", user, escaped)
    escaped = re.sub(r"&lt;#(\d+)&gt;", chan, escaped)
    escaped = re.sub(r"&lt;t:(-?\d+)(?::([tTdDfFR]))?&gt;", stamp, escaped)
    return re.sub(r"&lt;a?:(\w+):\d+&gt;", r":\1:", escaped)  # custom emoji -> :name:


def _render_inline(text: str, r: _Resolver) -> str:
    """Inline Discord markdown on raw text -> safe HTML (escaping happens first)."""
    slots: list[str] = []

    def stash(fragment: str) -> str:
        slots.append(fragment)
        return f"\x00{len(slots) - 1}\x00"

    s = html.escape(text, quote=True)
    # Protected first so nothing inside code or URLs gets formatted.
    s = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{m[1]}</code>"), s)
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
               lambda m: stash(f'<a href="{m[2]}" rel="noopener noreferrer">{m[1]}</a>'), s)
    s = re.sub(r"https?://[^\s<\x00]+[^\s<\x00.,:;!?)\]'\"]",
               lambda m: stash(f'<a href="{m[0]}" rel="noopener noreferrer">{m[0]}</a>'), s)
    s = _render_tokens(s, r, stash)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"__(.+?)__", r"<u>\1</u>", s)
    s = re.sub(r"\*(?!\s)(.+?)(?<!\s)\*", r"<em>\1</em>", s)
    s = re.sub(r"(?<![\w&])_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<em>\1</em>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    s = re.sub(r"\|\|(.+?)\|\|", r'<span class="spoiler">\1</span>', s)
    return re.sub(r"\x00(\d+)\x00", lambda m: slots[int(m[1])], s)


def _render_lines(text: str, r: _Resolver) -> str:
    """Block-level markdown (headings, subtext, quotes) for a chunk with no code fences."""
    out: list[str] = []
    quote: list[str] = []
    rest_quoted = False

    def flush() -> None:
        if quote:
            out.append(f"<blockquote>{'<br>'.join(quote)}</blockquote>")
            quote.clear()

    for line in text.split("\n"):
        if rest_quoted or line.startswith(">>> "):
            rest_quoted = True
            quote.append(_render_inline(line.removeprefix(">>> "), r))
            continue
        if line.startswith("> ") or line == ">":
            quote.append(_render_inline(line[2:], r))
            continue
        flush()
        for prefix, tag in (("### ", "h5"), ("## ", "h4"), ("# ", "h3")):
            if line.startswith(prefix):
                out.append(f"<{tag}>{_render_inline(line[len(prefix):], r)}</{tag}>")
                break
        else:
            if line.startswith("-# "):
                out.append(f'<div class="subtext">{_render_inline(line[3:], r)}</div>')
            else:
                out.append(_render_inline(line, r) + "<br>")
    flush()
    html_out = "".join(out)
    return html_out[:-4] if html_out.endswith("<br>") else html_out


def render_markdown(text: str | None, r: _Resolver) -> str:
    if not text:
        return ""
    parts = re.split(r"```(?:[\w+-]*\n)?(.*?)```", text, flags=re.S)
    return "".join(
        f"<pre><code>{html.escape(p.strip(chr(10)))}</code></pre>" if i % 2 else _render_lines(p, r)
        for i, p in enumerate(parts)
    )


def _fmt_size(n: int | None) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _render_embed(e: discord.Embed, r: _Resolver) -> str:
    color = e.colour.value if e.colour else 0x1E1F22
    parts = [f'<div class="embed" style="border-left-color:#{color:06x}">']
    if e.author and e.author.name:
        parts.append(f'<div class="embed-author">{html.escape(e.author.name)}</div>')
    if e.title:
        title = _render_inline(e.title, r)
        if e.url and str(e.url).startswith(("https://", "http://")):
            title = f'<a href="{html.escape(str(e.url))}" rel="noopener noreferrer">{title}</a>'
        parts.append(f'<div class="embed-title">{title}</div>')
    if e.description:
        parts.append(f'<div class="embed-desc">{render_markdown(e.description, r)}</div>')
    if e.fields:
        parts.append('<div class="fields">')
        for f in e.fields:
            cls = "field inline" if f.inline else "field"
            parts.append(
                f'<div class="{cls}"><div class="field-name">{_render_inline(f.name or "", r)}</div>'
                f'<div class="field-value">{render_markdown(f.value, r)}</div></div>'
            )
        parts.append("</div>")
    if e.image and e.image.url:
        parts.append(f'<img class="att-img" src="{html.escape(str(e.image.url))}" alt="">')
    if e.footer and e.footer.text:
        parts.append(f'<div class="embed-footer">{html.escape(e.footer.text)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _render_attachment(a) -> str:
    url = html.escape(str(getattr(a, "url", "") or ""))
    name = html.escape(getattr(a, "filename", "file") or "file")
    if (getattr(a, "content_type", "") or "").startswith("image/"):
        return f'<a href="{url}" rel="noopener noreferrer"><img class="att-img" src="{url}" alt="{name}"></a>'
    return (
        f'<div class="attachment">📄 <div><a href="{url}" rel="noopener noreferrer">{name}</a>'
        f'<div class="size">{_fmt_size(getattr(a, "size", None))}</div></div></div>'
    )


def _render_message(m: discord.Message, r: _Resolver, first: bool) -> str:
    author = m.author
    shown = getattr(author, "display_name", None) or author.name
    body = [f'<div class="content">{render_markdown(m.clean_content, r)}</div>'] if m.clean_content else []
    body += [_render_embed(e, r) for e in m.embeds]
    body += [_render_attachment(a) for a in m.attachments]
    body += [f'<div class="subtext">[Sticker: {html.escape(s.name)}]</div>' for s in getattr(m, "stickers", [])]
    exact = m.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    if not first:
        return (
            f'<div class="msg"><div class="gutter"><span class="hover-time" title="{exact}">'
            f'{m.created_at.strftime("%H:%M")}</span></div><div class="body">{"".join(body)}</div></div>'
        )
    avatar_url = getattr(getattr(author, "display_avatar", None), "url", None)
    img = f'<img src="{html.escape(str(avatar_url))}" alt="">' if avatar_url else ""
    color = _AVATAR_COLORS[getattr(author, "id", 0) % len(_AVATAR_COLORS)]
    badge = '<span class="badge">APP</span>' if getattr(author, "bot", False) else ""
    return (
        f'<div class="msg first"><div class="gutter"><div class="avatar" style="background:{color}">'
        f"{html.escape(shown[:1].upper())}{img}</div></div>"
        f'<div class="body"><span class="name" title="{html.escape(author.name)}">{html.escape(shown)}</span>{badge}'
        f'<span class="ts" title="{exact}">{m.created_at.strftime("%d/%m/%Y %H:%M")}</span>'
        f'{"".join(body)}</div></div>'
    )


def build_html_transcript(
    *,
    guild_name: str,
    channel_name: str,
    meta: list[tuple[str, str]],
    messages: list[discord.Message],
    resolver: _Resolver,
    generated_by: str = "Support Bot",
) -> str:
    rows, prev = [], None
    for m in messages:
        first = (
            prev is None
            or getattr(prev.author, "id", None) != getattr(m.author, "id", None)
            or m.created_at - prev.created_at > _GROUP_WINDOW
        )
        rows.append(_render_message(m, resolver, first))
        prev = m
    meta_html = "".join(f"<div><dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd></div>" for k, v in meta)
    title = f"#{channel_name}"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>Transcript • {html.escape(title)} • {html.escape(guild_name)}</title>"
        f"<style>{_TRANSCRIPT_CSS}</style></head><body>"
        f'<header class="header"><div class="server">{html.escape(guild_name)}</div>'
        f"<h1>{html.escape(title)}</h1><dl class=\"meta\">{meta_html}</dl></header>"
        f'<main class="messages">{"".join(rows) or "<p class=foot>No messages.</p>"}</main>'
        f'<footer class="foot">Generated by {html.escape(generated_by)} • {len(messages)} message(s) • '
        f"all times UTC</footer></body></html>\n"
    )
