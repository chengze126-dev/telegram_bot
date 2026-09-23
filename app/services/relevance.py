"""Business / development relevance classifier.

A transparent, keyword-based classifier. It only looks at text that is visible
in authorized groups (plus the public username / display name), never infers
sensitive attributes, and keeps at most a few short, redacted snippets as
evidence for why a score was given.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime

from app.database.models import RelevanceState

# Topic -> (weight, keyword regex fragments). Fragments are matched
# case-insensitively with token boundaries unless prefixed with "(?-i)".
TOPICS: dict[str, tuple[float, list[str]]] = {
    "Software Development": (10, [
        r"developers?", r"software (?:engineer(?:s|ing)?|developers?|development)", r"programm(?:er|ers|ing)",
        r"coding", r"coder", r"engineering team", r"dev team", r"open[- ]source", r"github", r"gitlab",
        r"code review", r"typescript", r"python", r"java", r"golang", r"c\+\+", r"c#", r"\.net", r"rust(?:lang)?",
        r"sdk", r"tech lead", r"cto",
    ]),
    "Web Development": (9, [
        r"web ?dev(?:elopment|eloper|elopers)?", r"websites?", r"web apps?", r"wordpress", r"shopify", r"webflow",
        r"html5?", r"landing pages?", r"web design(?:er)?", r"e-?commerce site",
    ]),
    "Mobile Development": (9, [
        r"mobile (?:apps?|dev(?:elopment|eloper|elopers)?)", r"ios", r"android", r"flutter", r"react native",
        r"swiftui", r"kotlin", r"app store", r"play store", r"xamarin",
    ]),
    "AI / ML": (11, [
        r"(?-i)AI", r"(?-i)ML", r"artificial intelligence", r"machine learning", r"deep learning", r"llms?",
        r"gpt(?:-?\d)?", r"chatgpt", r"openai", r"anthropic", r"claude", r"gemini", r"neural networks?", r"pytorch",
        r"tensorflow", r"data scien(?:ce|tists?)", r"nlp", r"computer vision", r"generative ai", r"gen ?ai",
        r"langchain", r"ai agents?", r"fine-?tun(?:e|ing)", r"embeddings?", r"mlops",
    ]),
    "SaaS": (11, [
        r"saas", r"micro-?saas", r"mrr", r"(?-i)ARR", r"churn", r"b2b", r"subscriptions? (?:business|model|app)",
        r"recurring revenue", r"paying customers",
    ]),
    "Startups": (11, [
        r"start-?ups?", r"founders?", r"co-?founders?", r"pre-?seed", r"seed round", r"series [abc]",
        r"venture capital", r"(?-i)VCs?", r"product[- ]market fit", r"(?-i)PMF", r"(?-i)MVP", r"y ?combinator",
        r"(?-i)YC", r"fundrais(?:e|ing)", r"investors?", r"angel invest(?:or|ors|ing)", r"bootstrapp(?:ed|ing)",
    ]),
    "Freelancing": (10, [
        r"freelanc(?:e|er|ers|ing)", r"upwork", r"fiverr", r"toptal", r"contractors?", r"for hire",
        r"hourly rate", r"side projects?", r"gigs?", r"consultan(?:t|ts|cy)",
    ]),
    "Remote Work": (8, [
        r"remote(?:ly)?", r"work from home", r"(?-i)WFH", r"fully distributed", r"digital nomads?",
        r"work from anywhere", r"async team",
    ]),
    "Hiring & Jobs": (12, [
        r"hiring", r"we(?:'re| are) hiring", r"jobs?", r"vacanc(?:y|ies)", r"recruit(?:er|ers|ing|ment)?",
        r"job openings?", r"open positions?", r"open roles?", r"apply (?:now|here)", r"resumes?", r"(?-i)CVs?",
        r"open to work", r"looking for (?:a |an )?(?:developer|engineer|dev|designer)s?", r"salary", r"interviews?",
        r"job offers?", r"career", r"headhunt(?:er|ing)",
    ]),
    "Business Collaboration": (10, [
        r"partnerships?", r"collaborat(?:e|ion|ions)", r"joint venture", r"looking for partners?",
        r"business development", r"bizdev", r"outsourc(?:e|ing)", r"agency", r"white[- ]label", r"clients?",
        r"lead gen(?:eration)?", r"(?-i)B2B", r"business proposal", r"let'?s connect",
    ]),
    "Product Development": (9, [
        r"product (?:manager|owner|management|design|designer|launch|roadmap|development)", r"roadmap",
        r"user research", r"(?-i)UX", r"ui/ux", r"figma", r"prototyp(?:e|es|ing)", r"feature requests?",
        r"user feedback", r"product hunt",
    ]),
    "DevOps": (10, [
        r"devops", r"ci/cd", r"docker", r"kubernetes", r"k8s", r"terraform", r"ansible", r"jenkins",
        r"github actions", r"helm", r"(?-i)SRE", r"observability", r"prometheus", r"grafana", r"infrastructure as code",
    ]),
    "Cloud Engineering": (10, [
        r"(?-i)AWS", r"azure", r"(?-i)GCP", r"google cloud", r"cloud (?:engineer|architect|native|infrastructure|computing|services)",
        r"serverless", r"lambda functions?", r"(?-i)EC2", r"(?-i)S3", r"cloudflare", r"digitalocean", r"heroku", r"vercel",
    ]),
    "Backend": (10, [
        r"back-?end", r"node(?:\.?js)?", r"express(?:\.?js)?", r"django", r"flask", r"fastapi", r"spring boot",
        r"laravel", r"php", r"ruby on rails", r"postgres(?:ql)?", r"mysql", r"mongodb", r"redis", r"graphql",
        r"rest(?:ful)? apis?", r"(?-i)APIs?", r"microservices?", r"nest\.?js", r"kafka",
    ]),
    "Frontend": (10, [
        r"front-?end", r"react(?:\.?js)?", r"vue(?:\.?js)?", r"angular", r"svelte", r"next\.?js", r"nuxt",
        r"javascript", r"tailwind", r"css", r"redux",
    ]),
    "Full-Stack": (11, [
        r"full[- ]?stack", r"(?-i)MERN", r"(?-i)MEAN stack", r"t3 stack",
    ]),
}

TOPIC_NAMES = list(TOPICS)

# Short profile tokens (from camelCase usernames like "JohnDev") that hint at a topic.
PROFILE_TOKENS = {
    "dev": "Software Development",
    "devs": "Software Development",
    "coder": "Software Development",
    "engineer": "Software Development",
    "js": "Frontend",
    "ai": "AI / ML",
    "ml": "AI / ML",
    "hr": "Hiring & Jobs",
    "jobs": "Hiring & Jobs",
    "founder": "Startups",
    "cto": "Software Development",
}

MAX_EVIDENCE = 3
SNIPPET_LEN = 160

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d")
_URL_RE = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[_\-.\d]+")


def _compile(fragment: str) -> re.Pattern[str]:
    flags = re.IGNORECASE
    if fragment.startswith("(?-i)"):
        fragment = fragment[len("(?-i)"):]
        flags = 0
    return re.compile(rf"(?<![\w#+.]){fragment}(?![\w#+])", flags)


_COMPILED: dict[str, list[re.Pattern[str]]] = {
    topic: [_compile(fragment) for fragment in keywords] for topic, (_, keywords) in TOPICS.items()
}


@dataclass
class TextAnalysis:
    topics: dict[str, list[str]]  # topic -> matched keywords
    first_span: tuple[int, int] | None

    @property
    def matched(self) -> bool:
        return bool(self.topics)


def redact(text: str) -> str:
    """Remove contact details and links so snippets never store personal data."""
    text = _EMAIL_RE.sub("[email]", text)
    text = _URL_RE.sub("[link]", text)
    text = _PHONE_RE.sub("[number]", text)
    return text


class RelevanceClassifier:
    def analyze_text(self, text: str) -> TextAnalysis:
        topics: dict[str, list[str]] = {}
        first: tuple[int, int] | None = None
        if not text:
            return TextAnalysis(topics, None)
        for topic, patterns in _COMPILED.items():
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    topics.setdefault(topic, []).append(match.group(0))
                    if first is None or match.start() < first[0]:
                        first = (match.start(), match.end())
        return TextAnalysis(topics, first)

    def profile_topics(self, username: str | None, first_name: str | None,
                       last_name: str | None, about: str | None = None) -> list[str]:
        parts = [p for p in (username, first_name, last_name) if p]
        tokens = " ".join(_CAMEL_RE.sub(" ", p) for p in parts)
        found = set(self.analyze_text(tokens).topics)
        for token in tokens.lower().split():
            if token in PROFILE_TOKENS:
                found.add(PROFILE_TOKENS[token])
        if about:
            found.update(self.analyze_text(about).topics)
        return sorted(found)

    @staticmethod
    def make_snippet(text: str, span: tuple[int, int] | None) -> str:
        text = redact(_WS_RE.sub(" ", text).strip())
        if len(text) <= SNIPPET_LEN:
            return text
        start = 0
        if span is not None:
            start = max(0, min(span[0] - SNIPPET_LEN // 3, len(text) - SNIPPET_LEN))
        snippet = text[start:start + SNIPPET_LEN].strip()
        prefix = "…" if start > 0 else ""
        suffix = "…" if start + SNIPPET_LEN < len(text) else ""
        return f"{prefix}{snippet}{suffix}"

    @staticmethod
    def score(topic_hits: dict[str, int], profile_topics: list[str] | None = None) -> int:
        raw = 0.0
        for topic, hits in topic_hits.items():
            if hits > 0 and topic in TOPICS:
                raw += TOPICS[topic][0] * math.log2(1 + hits)
        active = sum(1 for h in topic_hits.values() if h > 0)
        if active > 1:
            raw += 4 * min(active - 1, 6)
        if profile_topics:
            raw += 8 * min(len(profile_topics), 3)
        return max(0, min(100, round(100 * (1 - math.exp(-raw / 35)))))

    def apply_message(self, state: RelevanceState, text: str, message_date: datetime,
                      message_id: int, profile_topics: list[str]) -> bool:
        """Fold one visible group message into ``state``. Returns True if it was relevant."""
        analysis = self.analyze_text(text)
        if analysis.matched:
            for topic in analysis.topics:
                state.topic_hits[topic] = state.topic_hits.get(topic, 0) + 1
            state.supporting_messages_count += 1
            state.evidence.append({
                "snippet": self.make_snippet(text, analysis.first_span),
                "date": message_date.strftime("%Y-%m-%d"),
                "message_id": message_id,
                "topics": sorted(analysis.topics),
            })
            state.evidence.sort(key=lambda e: (len(e["topics"]), e["date"]), reverse=True)
            del state.evidence[MAX_EVIDENCE:]
        self.recompute(state, profile_topics)
        return analysis.matched

    def recompute(self, state: RelevanceState, profile_topics: list[str]) -> None:
        state.relevance_score = self.score(state.topic_hits, profile_topics)
        topics = {t for t, h in state.topic_hits.items() if h > 0} | set(profile_topics)
        state.detected_topics = sorted(topics, key=lambda t: (-state.topic_hits.get(t, 0), t))
