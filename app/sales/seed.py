"""Starter knowledge base and cadences, so the agent is useful on day one.

**Read this before going live.** These documents describe how Optimatech
positions itself, drawn from the marketing site's own product brief. They contain
**no prices and no numbers at all** — deliberately. Prices come from the live
catalogue through ``list_services``, durations from the calendar through
``check_call_availability``, and anything negotiated is escalated to a human. A
number written into prose here would go stale in silence and the agent would keep
quoting it with full confidence.

Every document ends with a review marker so it is obvious in the CRM which
content is seeded and which the team has written. Replace and extend these with
your own material — especially the case studies, which should name real clients.
"""

from typing import Any

from app.sales.enums import Audience, DocType

REVIEW_MARKER = "[محتوى مبدئي — للمراجعة من فريق Optimatech]"


def _doc(
    title: str,
    doc_type: DocType,
    audience: Audience,
    content: str,
    locale: str = "ar",
) -> dict[str, Any]:
    return {
        "title": title,
        "doc_type": doc_type.value,
        "audience": audience.value,
        "locale": locale,
        "content": content.strip() + f"\n\n{REVIEW_MARKER}",
    }


STARTER_DOCUMENTS: list[dict[str, Any]] = [
    _doc(
        "Optimatech — إحنا مين وبنعمل إيه",
        DocType.PRODUCT,
        Audience.ALL,
        """
Optimatech شركة تطوير رقمي مقرها بني سويف، بتخدم مصر ومنطقة الشرق الأوسط.

اللي بنبنيه:
• مواقع ومنصات ثنائية اللغة (عربي/إنجليزي) بدعم كامل للعربية من اليمين لليسار.
• لوحات تحكم وأنظمة إدارة محتوى مخصصة، بصلاحيات وأدوار، يديرها فريق العميل بنفسه.
• مساعدين بالذكاء الاصطناعي وأتمتة لتدفقات العمل والتقارير والتوجيه.
• أنظمة تشغيل للجمعيات والمؤسسات غير الهادفة للربح.
• إعداد Google Workspace: بريد بالدومين، تنظيم Drive، ترحيل بيانات، وتدريب.
• Optima LMS — منصة تعلّم عربية جاهزة، واحدة من منتجاتنا.

اللي يفرّقنا:
• العميل بيملك النتيجة: الكود والصلاحيات وتسليم موثّق. مش بنحتجز حد عندنا.
• الأسعار بالجنيه المصري ومعلنة، مش بالدولار ومخفية.
• عربي أولًا بجد — مش قالب أجنبي مترجم.
• بنثبت قدرتنا بشغل حقيقي تم تسليمه، مش بصفات وكلام كبير.
""",
    ),
    _doc(
        "Optimatech — who we are",
        DocType.PRODUCT,
        Audience.ALL,
        """
Optimatech is a digital agency based in Beni Suef, serving Egypt and the wider
MENA region.

What we build:
• Bilingual (Arabic/English) websites and platforms with real right-to-left support.
• Custom dashboards and content management systems with roles and permissions,
  designed for an Arabic-speaking team to run themselves.
• AI assistants and automation for workflows, reporting and routing.
• Operations systems for NGOs and non-profits.
• Google Workspace setup: domain email, Drive structure, data migration, training.
• Optima LMS — an Arabic-native learning platform, one of our own products.

What sets us apart:
• The client owns the result: the code, the access, and a documented handover.
• Prices are published in Egyptian pounds, not quoted in dollars and hidden.
• Genuinely Arabic-first, not a translated foreign template.
• We prove capability with real shipped work rather than adjectives.
""",
        locale="en",
    ),
    _doc(
        "سياسة التسعير وإزاي نتكلم عنها",
        DocType.PRICING,
        Audience.ALL,
        """
مهم: مفيش أي رقم في المستند ده بالمرة. أسعار الباقات بتتجاب من النظام مباشرة
وقت الكلام مع العميل.

القواعد:
• أي رقم بيتقال للعميل لازم يكون جاي من بيانات الباقات الحيّة، مش من الذاكرة.
• كل سعر معلن هو سعر **بداية**. النطاق والسعر النهائي بيتأكدوا كتابةً بعد مكالمة
  قصيرة. لازم نقول ده كل مرة نذكر فيها سعر.
• الدفع أونلاين من صفحة الدفع على الموقع، بالجنيه المصري.
• ينفع العميل يدفع دفعة مقدمة أو مبلغ متفق عليه من نفس الصفحة.
• أي خصم، أو سعر مخصص، أو عقد، أو فاتورة رسمية، أو مناقصة — بيتحوّل لموظف بشري
  فورًا. الوكيل ميوعدش بأي خصم وميتفاوضش.
• لو المشروع أكبر من أي باقة معلنة، ده تحويل لموظف مش تسعير.
• لو العميل سأل بالدولار: نقوله السعر بالجنيه ونسيبه يحوّل بنفسه.

ليه بنسعّر تحت السوق: القرار مقصود لكسب الشركات الصغيرة والمتوسطة الحساسة للسعر
في مصر. مش لازم نبرر السعر ولا نتكلم عن هوامشنا مع العميل.
""",
    ),
    _doc(
        "إزاي بنشتغل — من أول كلام لحد التسليم",
        DocType.PRODUCT,
        Audience.ALL,
        """
المسار المعتاد:

أولًا: مكالمة تعارف مجانية وقصيرة. بنفهم فيها العميل بيستخدم إيه دلوقتي وعايز
يوصل لإيه. مفيش أي التزام.

بعدها: عرض مكتوب فيه النطاق بالتفصيل والسعر النهائي. مفيش مفاجآت.

بعد الموافقة: اجتماع بدء مشروع، وبعده التنفيذ على مراحل مع مراجعات مع العميل.

التسليم: الكود والصلاحيات كلها بتنتقل للعميل، مع توثيق وتدريب لفريقه. العميل
يقدر يكمّل بنفسه أو مع أي حد تاني — مش مربوط بينا.

نقطة مهمة نقولها بصراحة: إحنا فريق محلي صغير بنشتغل على عدد محدود من المشاريع في
نفس الوقت. لو التوقيت مش متاح، نقول كده بدل ما نوعد ونتأخر.
""",
    ),
    _doc(
        "أسئلة متكررة",
        DocType.FAQ,
        Audience.ALL,
        """
س: بتشتغلوا مع مؤسسات صغيرة ولا الشركات الكبيرة بس؟
ج: الشركات الصغيرة والمتوسطة والجمعيات والستارتابس هما جمهورنا الأساسي.

س: الموقع هيكون بالعربي؟
ج: أيوه، عربي وإنجليزي بنفس الجودة، وبدعم كامل للاتجاه من اليمين لليسار. مش
ترجمة على قالب أجنبي.

س: الكود بيبقى ملكنا؟
ج: أيوه. الكود والصلاحيات بتنتقل لكم مع توثيق وتسليم رسمي.

س: بتستخدموا إيه تقنيًا؟
ج: بنبني بـ Next.js وTailwind للواجهات، وSupabase أو PostgreSQL للبيانات،
وموديلات Claude وGemini في الأتمتة. لو العميل عنده تفضيل معين نتكلم فيه.

س: فيه صيانة بعد التسليم؟
ج: ينفع نتفق على ترتيب للدعم والصيانة. التفاصيل بتتأكد مع الفريق.

س: بتشتغلوا من بره مصر؟
ج: أيوه، بنخدم منطقة الشرق الأوسط، والتعامل عن بُعد.

س: فيه شغل سابق نشوفه؟
ج: أيوه، فيه منصات حقيقية تم تسليمها لعملاء في قطاعات مختلفة — جمعيات، تعليم
صحي، استشارات بيئية، صحة عامة، تدريب مهني، وهوية بصرية. اسأل الفريق عن الأقرب
لمجالك.

س: بتدفعوا إزاي؟
ج: الدفع أونلاين من صفحة الدفع، بالجنيه المصري، وينفع دفعة مقدمة.
""",
    ),
    _doc(
        "الاعتراضات الشائعة والردود عليها",
        DocType.OBJECTION,
        Audience.ALL,
        """
اعتراض: "السعر عالي."
الرد: منجادلش. نسأل بيقارن بإيه بالظبط. الأغلب بيكون بيقارن بحد بيشتغل لوحده أو
بوكالة بتسعّر بالدولار. نرجع للقيمة: العميل بيملك الكود، والتسليم موثّق، والسعر
بالجنيه ومعلن. لو محتاج سعر مختلف عن المعلن → تحويل لموظف فورًا.

اعتراض: "لقيت حد أرخص."
الرد: محترم. نسأل الأرخص بيسلّم إيه بالظبط — الكود؟ توثيق؟ دعم بعد التسليم؟
منقللش من حد، بس نوضّح الفرق في اللي بيتسلّم.

اعتراض: "عندنا موقع بالفعل."
الرد: نسأل إيه اللي مضايقهم فيه دلوقتي. لو مفيش حاجة، منبيعش. الأغلب بيكون:
مش متجاوب على الموبايل، أو مش بيتحدّث لوحدهم، أو مش بالعربي كفاية.

اعتراض: "مش واثق في الشغل عن بُعد / في فريق مش معروف."
الرد: نعرض شغل حقيقي تم تسليمه لعملاء بأسمائهم، ونعرض مكالمة تعارف مجانية. الثقة
تتبنى بالتحديد مش بالوعود.

اعتراض: "الوقت ضيق عندي."
الرد: نحترم ده. نسأل إمتى يكون الوقت مناسب ونسجّله. لو قال مش دلوقتي، نسيبه في
حاله.

اعتراض: "ينفع تعملوا حاجة أكبر من الباقات المعلنة؟"
الرد: أيوه، وده بيتسعّر مخصص — تحويل لموظف بشري مباشرة.

اعتراض: "إنت روبوت؟"
الرد: نقول بصراحة إننا مساعد AI بنشتغل مع فريق Optimatech، وإن زميل من الفريق
ينفع يدخل في أي وقت.
""",
    ),
    _doc(
        "الجمعيات والمؤسسات غير الهادفة للربح",
        DocType.PRODUCT,
        Audience.B2B,
        """
كتير من الجمعيات اللي بنشتغل معاها بتبدأ من نفس المكان: إيميلات شخصية على Gmail،
ملفات متفرقة، قرارات ماشية على واتساب، وتقارير للمانحين بتتعمل يدوي كل مرة.

اللي بنعمله معاهم عادةً:
• تنظيم البريد والملفات على Google Workspace بالدومين بتاعهم، مع تدريب للفريق.
• نظام لسجلات المستفيدين والأنشطة بصلاحيات لكل موظف حسب دوره.
• تقارير بتتولّد لوحدها بدل ما تتجمّع يدوي.
• موقع بالعربي والإنجليزي يعرض الشغل للمانحين.

الكلام الصح مع جمعية: مش عن "التحول الرقمي"، لكن عن الوقت اللي بيروح في الشغل
اليدوي وإزاي يرجع للأنشطة نفسها. والميزانيات هنا بتكون محسوبة، فالصراحة في السعر
أهم من أي حاجة.
""",
    ),
    _doc(
        "قواعد الخصوصية في المحادثة",
        DocType.POLICY,
        Audience.ALL,
        """
• منطلبش رقم قومي، ولا رقم كارت، ولا بيانات بنكية، ولا كلمة سر، ولا كود OTP.
  أبدًا. لو العميل بعت أي حاجة من دي، منكرّرهاش ومنسجّلهاش.
• بنجمع بس اللي محتاجينه للتواصل والتأهيل: الاسم، وسيلة تواصل، جهة العمل ونوعها،
  الاحتياج، اللي مستخدمينه دلوقتي، التوقيت، والميزانية لو العميل قالها بنفسه.
• لو العميل قال "الغاء" أو "stop" أو أي طلب بوقف الرسايل، نوقف فورًا ونأكّد بسطر
  واحد بدون أي عرض بيعي.
• أي طلب بحذف بيانات أو سؤال عن الخصوصية بمعنى قانوني بيتحوّل لموظف بشري.
• منشاركش تفاصيل عميل مع عميل تاني، ولا نستخدم اسم عميل كمرجع من غير إذن.
""",
    ),
]


#: Follow-up cadences. Copy is free-form and is sent as plain text while the
#: service window is open; once it closes the sender falls back to the approved
#: re-engagement template configured in settings.
STARTER_SEQUENCES: list[dict[str, Any]] = [
    {
        "name": "b2b-followup",
        "description": (
            "Three touches over a week for an organisation that engaged but did "
            "not book. Stops the moment they reply or reach a goal stage."
        ),
        "audience": Audience.B2B.value,
        "steps": [
            {
                "delay_hours": 24,
                "channel": "WHATSAPP",
                "goal": "restart the conversation with something useful",
                "body": (
                    "اهلا {{name}}، أنا {{agent}} من {{product}}. "
                    "كنا بنتكلم على اللي محتاجينه — تحب أبعتلك اللي بيناسب "
                    "شغلكم بالظبط؟"
                ),
            },
            {
                "delay_hours": 72,
                "channel": "WHATSAPP",
                "goal": "offer the free discovery call",
                "body": (
                    "{{name}}، فيه مكالمة تعارف مجانية وقصيرة لو حابب نفهم "
                    "الصورة أكتر ونقولك رأينا بصراحة. يوم إيه يناسبك؟"
                ),
            },
            {
                "delay_hours": 168,
                "channel": "WHATSAPP",
                "goal": "close the loop politely",
                "body": (
                    "{{name}}، مش عايز أزعجك. أقفل الموضوع دلوقتي وأرجعلك بعدين، "
                    "ولا تحب نكمل؟"
                ),
            },
        ],
    },
    {
        "name": "b2c-followup",
        "description": (
            "Two touches for an individual who asked about a service and did not "
            "move."
        ),
        "audience": Audience.B2C.value,
        "steps": [
            {
                "delay_hours": 24,
                "channel": "WHATSAPP",
                "goal": "answer the remaining question",
                "body": (
                    "اهلا {{name}}، أنا {{agent}} من {{product}}. "
                    "فيه حاجة لسه مش واضحة في اللي كنت بتسأل عليه؟"
                ),
            },
            {
                "delay_hours": 96,
                "channel": "WHATSAPP",
                "goal": "last useful nudge",
                "body": (
                    "{{name}}، لو حابب نتكلم مكالمة قصيرة مجانية ونقولك رأينا "
                    "بصراحة قبل أي قرار، قولي وأرتّبها."
                ),
            },
        ],
    },
]
