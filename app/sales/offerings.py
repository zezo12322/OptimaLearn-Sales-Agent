"""What Optimatech sells.

Mirrors ``lib/services.ts`` in the marketing site, which is the source of truth
for the packages shown on the pricing section and the checkout. Kept as data in
this repo rather than fetched over HTTP for one reason: the agent must be able to
answer "how much is a website?" even when the site is mid-deploy, and a four-row
catalogue that changes a few times a year does not justify a network dependency
on the critical path of every conversation.

The trade-off is that the two lists can drift, so :func:`verify_against_site`
exists to catch that in CI, and every price the agent quotes is framed the way
the business actually sells: a *starting* figure, with the real scope confirmed
in writing after a discovery call.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode

from app.core.config import settings

logger = logging.getLogger(__name__)

CURRENCY = "EGP"

#: Bounds on a custom deposit / invoice payment, from ``lib/services.ts``.
MIN_CUSTOM_EGP = 500
MAX_CUSTOM_EGP = 1_000_000


@dataclass(frozen=True)
class Service:
    slug: str
    title_en: str
    title_ar: str
    tagline_en: str
    tagline_ar: str
    #: "Starting from" price in EGP. Never presented as a final quote.
    price_egp: int
    features_en: list[str] = field(default_factory=list)
    features_ar: list[str] = field(default_factory=list)
    popular: bool = False

    def title(self, locale: str) -> str:
        return self.title_ar if locale.startswith("ar") else self.title_en

    def tagline(self, locale: str) -> str:
        return self.tagline_ar if locale.startswith("ar") else self.tagline_en

    def features(self, locale: str) -> list[str]:
        return self.features_ar if locale.startswith("ar") else self.features_en


SERVICES: tuple[Service, ...] = (
    Service(
        slug="marketing-site",
        title_en="Bilingual marketing website",
        title_ar="موقع تسويقي ثنائي اللغة",
        tagline_en=(
            "A fast, professional EN/AR site your team owns — designed, built, "
            "and handed over."
        ),
        tagline_ar=(
            "موقع احترافي سريع بالعربي والإنجليزي يملكه فريقك — تصميم وتنفيذ "
            "وتسليم كامل."
        ),
        price_egp=11_900,
        features_en=[
            "Up to 6 bilingual, RTL-native pages",
            "Next.js + Tailwind, responsive & fast",
            "Contact form + WhatsApp integration",
            "Documented handover — you own the code",
        ],
        features_ar=[
            "حتى 6 صفحات ثنائية اللغة وداعمة للعربية",
            "Next.js + Tailwind، متجاوب وسريع",
            "فورم تواصل + ربط واتساب",
            "تسليم موثّق — الكود ملكك",
        ],
    ),
    Service(
        slug="cms-dashboard",
        title_en="Custom CMS & dashboard",
        title_ar="لوحة تحكم ونظام إدارة محتوى",
        tagline_en=(
            "A Supabase-backed admin panel with role-based access your Arabic "
            "team manages itself."
        ),
        tagline_ar=(
            "لوحة إدارة على Supabase بصلاحيات أدوار يديرها فريقك العربي بنفسه."
        ),
        price_egp=29_900,
        features_en=[
            "Role-based access control",
            "Arabic-first admin UX",
            "Content, records & reporting",
            "Training + handover for your team",
        ],
        features_ar=[
            "صلاحيات وأدوار للمستخدمين",
            "واجهة إدارة عربية أولًا",
            "محتوى وسجلات وتقارير",
            "تدريب وتسليم لفريقك",
        ],
        popular=True,
    ),
    Service(
        slug="ai-automation",
        title_en="AI assistant & automation",
        title_ar="مساعد ذكي وأتمتة",
        tagline_en=(
            "Claude- and Gemini-powered workflows that cut manual hours out of "
            "your operations."
        ),
        tagline_ar=(
            "تدفقات عمل مدعومة بـ Claude وGemini توفّر ساعات يدوية من عملياتك."
        ),
        price_egp=13_900,
        features_en=[
            "Workflow & prompt design",
            "Integrations with your tools",
            "Reporting & routing automations",
            "Documentation + team training",
        ],
        features_ar=[
            "تصميم تدفقات وprompts",
            "تكاملات مع أدواتك",
            "أتمتة تقارير وتوجيه",
            "توثيق + تدريب الفريق",
        ],
    ),
    Service(
        slug="workspace-setup",
        title_en="Google Workspace setup",
        title_ar="إعداد Google Workspace",
        tagline_en=(
            "Domain email, Drive structure, migration, and practical Arabic "
            "team training."
        ),
        tagline_ar="بريد بالدومين، تنظيم Drive، ترحيل، وتدريب عملي للفريق بالعربي.",
        price_egp=3_500,
        features_en=[
            "Domain email + DNS setup",
            "Drive structure & permissions",
            "Data migration",
            "Hands-on Arabic training",
        ],
        features_ar=[
            "بريد بالدومين + إعداد DNS",
            "تنظيم Drive والصلاحيات",
            "ترحيل البيانات",
            "تدريب عملي بالعربي",
        ],
    ),
)

BY_SLUG = {service.slug: service for service in SERVICES}

#: Free-text hints mapped onto a package, so "عايز موقع" reaches the right one
#: without an embedding round-trip. Arabic and English, including the words
#: people actually use rather than the product names.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "marketing-site": (
        "website", "web site", "landing", "site", "pages", "portfolio",
        "موقع", "ويب", "صفحة", "صفحات", "لاندينج", "بورتفوليو",
    ),
    "cms-dashboard": (
        "dashboard", "admin", "cms", "panel", "system", "database", "records",
        "erp", "portal", "internal",
        "لوحة", "تحكم", "ادارة", "إدارة", "نظام", "داشبورد", "سجلات",
        "قاعدة بيانات", "بورتال",
    ),
    "ai-automation": (
        "ai", "automation", "bot", "chatbot", "agent", "workflow", "integrate",
        "whatsapp bot", "claude", "gemini", "gpt",
        "ذكاء", "اصطناعي", "أتمتة", "اتمتة", "بوت", "شات", "وكيل", "تكامل",
        "روبوت",
    ),
    "workspace-setup": (
        "workspace", "email", "gmail", "drive", "google", "domain", "migration",
        "ايميل", "إيميل", "بريد", "دومين", "درايف", "جوجل", "ترحيل",
    ),
}


def search(query: str, limit: int = 4) -> list[Service]:
    """Rank the catalogue against free text.

    Keyword scoring, not embeddings: four packages with distinct vocabularies
    make a term match both accurate and instant, and it avoids an API call in
    the middle of a WhatsApp reply.
    """
    text = (query or "").lower()
    if not text.strip():
        return list(SERVICES[:limit])

    scored: list[tuple[int, Service]] = []
    for service in SERVICES:
        score = sum(1 for word in _KEYWORDS.get(service.slug, ()) if word in text)
        # A direct hit on the package name outranks incidental keyword overlap.
        if service.slug.replace("-", " ") in text or service.title_en.lower() in text:
            score += 5
        if service.title_ar in query:
            score += 5
        if score:
            scored.append((score, service))

    scored.sort(key=lambda pair: (-pair[0], pair[1].price_egp))
    return [service for _score, service in scored[:limit]]


def get(slug: str) -> Optional[Service]:
    return BY_SLUG.get((slug or "").strip().lower())


def _site_url(path: str, params: Optional[dict[str, str]] = None) -> Optional[str]:
    """Absolute site URL, locale-aware. ``None`` when the site root is unset."""
    if not settings.site_base_url:
        return None
    base = settings.site_base_url.rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urlencode(params)}"
    return url


def _localized_path(path: str, locale: str) -> str:
    """The site serves Arabic under an ``/ar`` prefix."""
    return f"ar/{path.lstrip('/')}" if locale.startswith("ar") else path


def checkout_url(locale: str = "ar", package_slug: Optional[str] = None) -> Optional[str]:
    """Paymob checkout page, optionally preselecting a package."""
    params = {"package": package_slug} if package_slug else None
    return _site_url(_localized_path("checkout", locale), params)


def booking_url(locale: str = "ar", call_type: Optional[str] = None) -> Optional[str]:
    """Booking page, optionally preselecting a call type."""
    params = {"type": call_type} if call_type else None
    return _site_url(_localized_path("book", locale), params)


def pricing_url(locale: str = "ar") -> Optional[str]:
    section = _localized_path("", locale).rstrip("/")
    base = settings.site_base_url.rstrip("/") if settings.site_base_url else None
    if not base:
        return None
    return f"{base}/{section}#pricing" if section else f"{base}/#pricing"


def format_price(amount: int, locale: str = "ar") -> str:
    """Thousands-separated price with the currency the business actually quotes."""
    formatted = f"{amount:,}"
    return f"{formatted} جنيه" if locale.startswith("ar") else f"{formatted} EGP"


def as_payload(service: Service, locale: str = "ar") -> dict[str, Any]:
    """Tool-result shape. Includes the framing, so the model cannot drop it."""
    return {
        "slug": service.slug,
        "title": service.title(locale),
        "summary": service.tagline(locale),
        "starting_price": service.price_egp,
        "starting_price_formatted": format_price(service.price_egp, locale),
        "currency": CURRENCY,
        "includes": service.features(locale),
        "most_popular": service.popular,
        "price_is_starting_point": True,
        "checkout_url": checkout_url(locale, service.slug),
    }


def custom_payment_bounds() -> dict[str, int]:
    return {"min_egp": MIN_CUSTOM_EGP, "max_egp": MAX_CUSTOM_EGP}


def verify_against_site(site_packages: list[dict[str, Any]]) -> list[str]:
    """Compare this catalogue with the site's ``SERVICE_PACKAGES``.

    Returns a list of human-readable differences, empty when they agree. Called
    by a test that parses ``lib/services.ts`` when the site checkout is available
    in the same checkout, so a price change on the site cannot silently leave the
    agent quoting a stale figure.
    """
    problems: list[str] = []
    site_by_slug = {
        str(entry.get("slug")): entry for entry in site_packages if entry.get("slug")
    }

    for slug in set(site_by_slug) | set(BY_SLUG):
        mine = BY_SLUG.get(slug)
        theirs = site_by_slug.get(slug)
        if mine is None:
            problems.append(f"{slug}: on the site but missing from the agent")
            continue
        if theirs is None:
            problems.append(f"{slug}: in the agent but no longer on the site")
            continue
        site_price = theirs.get("price_egp")
        if site_price is not None and int(site_price) != mine.price_egp:
            problems.append(
                f"{slug}: site says {site_price} EGP, agent says {mine.price_egp} EGP"
            )
        for key, value in (("title_en", mine.title_en), ("title_ar", mine.title_ar)):
            if theirs.get(key) and theirs[key] != value:
                problems.append(
                    f"{slug}.{key}: site says {theirs[key]!r}, agent says {value!r}"
                )
    return sorted(problems)
