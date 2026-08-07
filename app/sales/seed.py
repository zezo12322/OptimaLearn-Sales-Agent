"""Starter knowledge base and cadences, so the agent is useful on day one.

**Read this before going live.** The documents below describe only what is
actually shipped in the platform today, and they deliberately contain **no
prices and no numbers**: pricing comes from the live plans through the
``get_pricing`` tool, and anything negotiated is escalated to a human. Every
document ends with a review marker so it is obvious in the CRM which content is
seeded and which the team has written.

Replace and extend these with your own material — the seed exists so the agent
never has to answer from an empty knowledge base, not as a substitute for the
team's own positioning.
"""

from typing import Any

from app.sales.enums import Audience, DocType

REVIEW_MARKER = "[محتوى مبدئي — للمراجعة من فريق OptimaLearn]"


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
        "نظرة عامة على منصة OptimaLearn",
        DocType.PRODUCT,
        Audience.ALL,
        """
OptimaLearn منصة تعلّم وتطوير مهني بالعربي والإنجليزي، متاحة على الويب وعلى
تطبيق موبايل (iOS و Android)، وبواجهة عربية كاملة من اليمين لليسار.

اللي المنصة بتعمله فعليًا اليوم:

• كورسات منظّمة في أقسام ومحاضرات، مع كتالوج وتصنيفات وبحث بعنوان الكورس.
• تشغيل فيديو مع تتبّع تقدّم دقيق: التقدّم بيتحدّث كل ثوانٍ والإكمال بيتحدّد من
  جهة السيرفر، فالتقارير بتعكس مشاهدة حقيقية.
• اختبارات مصغّرة (Quizzes) بتصحيح آلي، مؤقّت بيتفرض من السيرفر، ودرجة نجاح.
• مهام عملية (Optima Missions) بتتصحّح بمعايير (Rubric) وبتتسجّل درجاتها
  ومراجعات المدرّب عليها.
• مساعد ذكاء اصطناعي داخل كل محاضرة بيجاوب على أسئلة الدارس من محتوى الفيديو
  نفسه مع الرجوع لتوقيت الدقيقة في الفيديو، بالعربي أو الإنجليزي.
• ملف شخصي للدارس فيه إحصائياته وتقدّمه.

المنصة مبنية على أساس أمني واضح: صلاحيات بالأدوار، تشفير للبيانات الحساسة،
حماية للفيديو بروابط مؤقتة موقّعة وتشفير للمقاطع، وتحديد لمعدّل الطلبات.
""",
    ),
    _doc(
        "OptimaLearn platform overview",
        DocType.PRODUCT,
        Audience.ALL,
        """
OptimaLearn is a professional learning platform available in Arabic and English,
on the web and as a mobile app (iOS and Android), with full right-to-left Arabic
support.

What the platform does today:

• Courses organised into sections and lectures, with a catalogue, categories and
  title search.
• Video playback with granular progress tracking. Completion is derived
  server-side, so reporting reflects real watch time rather than a client claim.
• Quizzes with automatic grading, a server-enforced timer and a pass mark.
• Practical assignments (Optima Missions) graded against a rubric, with
  per-criterion scores and instructor feedback.
• An AI tutor inside each lecture that answers questions from that lecture's own
  content and cites the timestamp it came from, in Arabic or English.
• A learner profile with personal progress and statistics.

Security is built in: role-based access control, encryption of sensitive personal
data, protected video delivery through short-lived signed links and encrypted
segments, and request rate limiting.
""",
        locale="en",
    ),
    _doc(
        "سياسة التسعير وكيف نتكلم عنها",
        DocType.PRICING,
        Audience.ALL,
        """
مهم: الأسعار مش مكتوبة في المستند ده بالمرة. الباقات وأسعارها الحقيقية بتتجاب
من النظام مباشرة وقت الكلام مع العميل.

القواعد:
• أي رقم بيتقال للعميل لازم يكون جاي من بيانات الباقات الحيّة، مش من الذاكرة.
• الاشتراك بيتدفع أونلاين من داخل المنصة عن طريق بوابة دفع إلكتروني.
• طرق دفع إضافية (فوري / إنستاباي) على خطة التطوير، فمينفعش نتعامل معاها
  كمتاحة حاليًا.
• أي خصم، أو سعر خاص، أو عرض لفريق كبير، أو تقسيط، أو فاتورة رسمية، أو عقد —
  كل ده بيتحوّل لموظف بشري فورًا. الوكيل ميوعدش بأي خصم.
• لو العميل سأل عن سعر ومفيش بيانات باقات متاحة في اللحظة، نقول إننا هنبعتله
  التفاصيل بالظبط ونحوّله لحد من الفريق.

لو عميل شركة سأل عن تكلفة عدد مستخدمين: ينفع نعرض حساب تقريبي مبني على سعر
الباقة المعلن × عدد المستخدمين، مع توضيح صريح إنه رقم استرشادي والسعر النهائي
بيتأكد من الفريق.
""",
    ),
    _doc(
        "تدريب فرق الشركات — الوضع الحالي",
        DocType.PRODUCT,
        Audience.B2B,
        """
للشركات اللي عايزة تدرّب فريقها على OptimaLearn:

المتاح دلوقتي:
• إنشاء حسابات للموظفين وتسجيلهم على كورسات محددة، بإدارة من فريقنا مع مسؤول
  التدريب في الشركة.
• متابعة تقدّم كل دارس (نسبة المشاهدة، إكمال المحاضرات، درجات الاختبارات
  والمهام) من لوحة الإدارة.
• محتوى بالعربي والإنجليزي على الويب والموبايل، فمينفعش يكون اللغة عائق.
• إمكانية بناء كورسات خاصة بالشركة ومهام عملية مرتبطة بشغلها الفعلي.

على خطة التطوير (لسه مش متاح — منقولوش إنه موجود):
• لوحة تحكم self-service لمسؤول الشركة فيها تحليل فجوات المهارات وتقدّم الفريق.
• استيراد الموظفين بملف CSV ومجموعات للشركات.
• تقارير الحضور والتصدير.

الطريقة الصح في الكلام: نوضّح اللي متاح دلوقتي بصراحة، ولو الشركة محتاجة حاجة
من اللي على الخطة، نحوّلها لموظف بشري يتكلم معاها في التوقيتات.
""",
    ),
    _doc(
        "أسئلة متكررة",
        DocType.FAQ,
        Audience.ALL,
        """
س: المنصة بالعربي؟
ج: أيوه، واجهة عربية كاملة RTL على الويب والموبايل، وفيه إنجليزي كمان، والدارس
يقدر يبدّل اللغة.

س: فيه تطبيق موبايل؟
ج: أيوه، تطبيق للـ iOS والأندرويد فيه تشغيل الكورسات ومتابعة التقدّم.

س: الشهادات؟
ج: النظام بيسجّل إنجاز الدارس ودرجاته. تفاصيل إصدار الشهادات بالظبط بيأكدها
موظف من الفريق.

س: ينفع أجرّب قبل الشراء؟
ج: فيه محاضرات معاينة مجانية في الكورسات. لو حابب عرض توضيحي (Demo) نرتّبه.

س: المحتوى محمي؟
ج: أيوه. الفيديو بيتشغّل بروابط مؤقتة موقّعة والمقاطع مشفّرة، والتخزين مقفول
افتراضيًا. مفيش تحميل مباشر للفيديو.

س: فيه مساعد ذكاء اصطناعي؟
ج: أيوه. جوّه كل محاضرة فيه مساعد بيجاوب من محتوى المحاضرة نفسها وبيقولك
الدقيقة اللي الإجابة فيها، بالعربي أو الإنجليزي.

س: بياناتنا آمنة؟
ج: الصلاحيات بالأدوار، والبيانات الشخصية الحساسة مشفّرة، وفيه سجل للعمليات
الحساسة. أي سؤال قانوني أو تعاقدي عن الخصوصية بيتحوّل لموظف بشري.
""",
    ),
    _doc(
        "الاعتراضات الشائعة والردود عليها",
        DocType.OBJECTION,
        Audience.ALL,
        """
اعتراض: "السعر عالي."
الرد: منجادلش في السعر. نسأل بالظبط هو بيقارن بإيه، ونرجع للقيمة: تتبّع تقدّم
حقيقي، اختبارات ومهام بتصحيح، ومساعد AI جوّه المحاضرة. لو محتاج سعر مختلف عن
المعلن → تحويل لموظف بشري فورًا.

اعتراض: "إحنا مستخدمين منصة تانية."
الرد: نسأل إيه الناقص في اللي مستخدمينه. الأغلب بيكون: محتوى عربي حقيقي،
متابعة تقدّم مش شكلية، أو مهام عملية بتتصحّح. نتكلم في النقطة اللي وجعته بس.

اعتراض: "الموظفين مش هيستخدموها."
الرد: نسأل جرّبوا إيه قبل كده وإيه اللي حصل. نتكلم عن إن التطبيق بالعربي وعلى
الموبايل، وإن التقدّم بيتقاس فعليًا فمسؤول التدريب يقدر يشوف مين ماشي ومين لأ.

اعتراض: "مش وقته دلوقتي."
الرد: نحترم ده. نسأل إمتى يكون الوقت مناسب، ونسجّله، ونسأل لو ينفع نبعتله
حاجة مفيدة لحد الوقت ده. لو قال لأ، نسيبه في حاله.

اعتراض: "عايز أتكلم مع حد."
الرد: نحوّل فورًا بدون أي مناقشة أو محاولة إقناع.

اعتراض: "إنت روبوت؟"
الرد: نقول بصراحة إننا مساعد AI بنشتغل مع فريق OptimaLearn، وإن زميل من الفريق
ينفع يدخل في أي وقت.
""",
    ),
    _doc(
        "قواعد الخصوصية في المحادثة",
        DocType.POLICY,
        Audience.ALL,
        """
• منطلبش رقم قومي، ولا رقم كارت، ولا بيانات بنكية، ولا كلمة سر، ولا كود OTP.
  أبدًا. لو العميل بعت أي حاجة من دي، منكرّرهاش ومنسجّلهاش.
• بنجمع بس اللي محتاجينه للتواصل والتأهيل: الاسم، وسيلة تواصل، الشركة، الاحتياج،
  التوقيت، والميزانية لو العميل قالها بنفسه.
• لو العميل قال "الغاء" أو "stop" أو أي طلب بوقف الرسايل، نوقف فورًا ونأكّد
  بسطر واحد بدون أي عرض بيعي.
• أي طلب بحذف بيانات أو سؤال عن الخصوصية بمعنى قانوني بيتحوّل لموظف بشري.
""",
    ),
]


#: Follow-up cadences. Copy is free-form and is sent as plain text while the
#: 24-hour service window is open; once it closes the sender falls back to the
#: approved re-engagement template configured in settings.
STARTER_SEQUENCES: list[dict[str, Any]] = [
    {
        "name": "b2b-followup",
        "description": (
            "Three touches over a week for team buyers who engaged but did not "
            "book. Stops the moment they reply or reach a goal stage."
        ),
        "audience": Audience.B2B.value,
        "steps": [
            {
                "delay_hours": 24,
                "channel": "WHATSAPP",
                "goal": "restart the conversation with something useful",
                "body": (
                    "اهلا {{name}}، أنا {{agent}} من {{product}}. "
                    "كنت بسألك عن تدريب الفريق — تحب أبعتلك الكورسات اللي "
                    "بتناسب شغلكم بالظبط؟"
                ),
            },
            {
                "delay_hours": 72,
                "channel": "WHATSAPP",
                "goal": "offer a short demo",
                "body": (
                    "{{name}}، لو ينفع ربع ساعة عرض سريع على {{product}} "
                    "أوريك إزاي بتتابع تقدّم الفريق فعليًا. يوم إيه يناسبك؟"
                ),
            },
            {
                "delay_hours": 168,
                "channel": "WHATSAPP",
                "goal": "close the loop politely",
                "body": (
                    "{{name}}، مش عايز أزعجك. أقفل الموضوع دلوقتي وأرجعلك "
                    "بعدين، ولا تحب نكمل؟"
                ),
            },
        ],
    },
    {
        "name": "b2c-followup",
        "description": (
            "Two touches for individual learners who asked about a course and "
            "did not enrol."
        ),
        "audience": Audience.B2C.value,
        "steps": [
            {
                "delay_hours": 24,
                "channel": "WHATSAPP",
                "goal": "answer the remaining question",
                "body": (
                    "اهلا {{name}}، أنا {{agent}} من {{product}}. "
                    "فيه حاجة لسه مش واضحة في الكورس اللي كنت بتسأل عليه؟"
                ),
            },
            {
                "delay_hours": 96,
                "channel": "WHATSAPP",
                "goal": "last useful nudge",
                "body": (
                    "{{name}}، فيه محاضرات معاينة مجانية تقدر تجرّبها قبل أي "
                    "قرار. أبعتلك اللينك؟"
                ),
            },
        ],
    },
]
