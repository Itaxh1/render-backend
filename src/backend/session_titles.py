"""Task names are separate from outcome summaries and never require an LLM."""
import re

SETUP = re.compile(r'^(?:#{1,6}\s*)?(?:(?:AGENTS|CLAUDE)\.md\s+instructions\b|<(?:instructions|environment_context|system-reminder|permissions instructions|turn_aborted)\b|<!--\s*context7\b)', re.I)


def real_prompt(text):
    if not text:
        return None
    text = re.sub(r'^\s*Remote Control is active[^\r\n]*(?:\r?\n|$)', '', text, flags=re.I).strip()
    if not text or SETUP.match(text):
        return None
    text = re.sub(r'\s+', ' ', re.sub(r'^\s*#{1,6}\s*', '', text)).strip()
    if re.fullmatch(r'(continue|proceed|yes|ok(?:ay)?|thanks|go ahead)[.!\s]*', text, re.I):
        return None
    return text or None


def task_title(text):
    text = real_prompt(text)
    if not text:
        return None
    text = re.sub(r'^(?:(?:can|could|would) you\s+)?(?:please\s+)?(?:help me\s+)?', '', text, flags=re.I)
    text = re.split(r'[\n!?]|(?<=[a-z])\.\s', text, maxsplit=1)[0].strip(' "`*')
    words = text.split()[:8]
    while words and len(' '.join(words)) > 64:
        words.pop()
    title = ' '.join(words).rstrip(' ,:;.-')
    return title[0].upper() + title[1:] if title else None


TITLE_UPDATE = """
update public.sessions set display_title=%s,title_origin='prompt',title_prompt_at=%s
where user_id=%s and id=%s and
  (display_title is null or (title_origin='prompt' and (title_prompt_at is null or title_prompt_at>%s)))
"""
