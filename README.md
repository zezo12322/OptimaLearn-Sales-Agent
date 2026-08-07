# OptimaLearn Sales Agent

وكيل مبيعات بالذكاء الاصطناعي لمنصة **OptimaLearn** — بيشتغل على **واتساب** و**فيسبوك ماسنجر**، بيرد على العملاء المحتملين بالعربي والإنجليزي، بيأهّلهم، وبيسلّمهم للسيلز، وبيعمل متابعة (follow-up) بشكل متوافق مع سياسات Meta.

خدمة مستقلة (FastAPI + Celery + PostgreSQL/pgvector + Azure OpenAI). بتتكلم مع الـ LMS عن طريق HTTP بس — مفيهاش أي وصول مباشر لقاعدة بيانات الـ LMS.

---

## اللي الوكيل بيعمله

**1. Inbound — يرد ويأهّل**
عميل بيبعت على واتساب أو ماسنجر → الوكيل بيرد بلغته، بيجاوب من قاعدة معرفة الشركة (مش من ذاكرته)، بيرشّح كورسات من الكتالوج الحقيقي، ويجمع الـ qualification (الاحتياج، فرد أو فريق، عدد المستخدمين، التوقيت، مين بياخد القرار، الميزانية) — سؤال واحد في المرة، مش استجواب.

**2. Outbound — يجيب عملاء بالطرق المسموحة**
- **Comment-to-DM:** أي تعليق على بوست أو إعلان على صفحة الفيسبوك → رد خاص (private reply) واحد مسموح من Meta. ده أقوى مصدر محادثات حقيقي.
- **Facebook Lead Ads:** فورم اتقدّم → الوكيل بيجيب بياناته من Graph API ويبعت أول رسالة على واتساب بـ template معتمد.
- **Click-to-WhatsApp ads:** بيتسجّل الإعلان اللي جاب المحادثة، فتقدر تعرف أي creative بيجيب leads فعلًا.
- **Cadences:** متابعة على خطوات للـ leads اللي عملوا opt-in، وبتتوقف فورًا أول ما العميل يرد.

**3. In-product upsell**
الـ LMS بيبعت إشارات استخدام (كورسات خلصت، محاضرات، وصل لحد الباقة) → الوكيل يرجّع اقتراح ترقية بصياغة شخصية، أو **مايرجّعش حاجة** لو الإشارات ضعيفة أو العميل بيدفع بالفعل.

---

## ⚠️ الالتزام بسياسات Meta — مش اختياري

الرسايل الجماعية الباردة على واتساب/ماسنجر **تحرق الرقم والصفحة**. الخدمة دي مبنية بحيث ده مايحصلش:

| القاعدة | التنفيذ |
|---|---|
| مفيش رسالة لحد ملوش أساس قانوني | `NO_CONSENT` deny — لازم يكون بعت لنا، أو عمل opt-in صريح، أو قدّم فورم |
| نافذة الـ 24 ساعة | داخلها: نص عادي. برّاها على واتساب: template معتمد بس. برّاها على ماسنجر: **مفيش** — بنستنى العميل |
| Opt-out فوري ونهائي | كلمة واحدة (`stop` / `الغاء` / `مش عايز`...) → توقف كل حاجة، وبتلغي أي cadence شغّال |
| Quiet hours | مفيش رسالة proactive بين 9م و 9ص بتوقيت العميل |
| سقف رسايل | رسالة proactive واحدة في اليوم، و5 على مدى العمر (قابل للتعديل) |
| كل رفض متسجّل | صف في `sales_outbound_messages` بـ `SKIPPED` و`skip_reason` — الرفض موثّق زي الإرسال |

الردود على العملاء اللي بيسألوا **مش** بتتأثر بالـ quiet hours ولا بالسقوف — تأخير رد على سؤال حيّ خدمة أسوأ، مش التزام أحسن.

---

## المعمارية

```
Meta (WhatsApp / Messenger / Page)
        │  webhook (X-Hub-Signature-256)
        ▼
  FastAPI  /v1/sales/webhooks/*        ← السطح العام الوحيد
        │  Celery (Redis)
        ▼
  inbound.handle_event
        ├─ repository   → حلّ هوية العميل عبر القنوات + تسجيل الرسائل (idempotent)
        ├─ policy       → مسموح نرد؟ مسموح نبعت؟
        ├─ agent        → tool-calling loop محدود
        │     ├─ kb          → RAG على قاعدة معرفة المبيعات (pgvector)
        │     ├─ lms         → الكتالوج والأسعار الحيّة (HTTP)
        │     └─ tools       → حفظ بيانات، حجز demo، تحويل لموظف
        ├─ qualification → score حسابي + مرحلة في الـ pipeline
        └─ outbound     → queue + cadences (Celery beat)

  FastAPI  /v1/sales/*  (X-Internal-Api-Key)  ← الـ LMS بس
```

**اللي يستاهل الانتباه:**

- **الـ policy engine نقي تمامًا** (`app/sales/policy.py`) — مفيهوش DB ولا HTTP، فكل قاعدة متغطّية باختبار.
- **الـ scoring حسابي مش موديل** (`app/sales/qualification.py`) — الـ rep يشوف بالظبط ليه الـ lead واصل 78، والرقم مايتغيرش لما الـ prompt يتعدّل.
- **الأسعار مبتتخزّنش هنا** — بتتجاب من الـ LMS وقت الرد. أي رقم في المستندات بيبوظ في صمت.
- **الموديل ميقدرش يعدّل الـ stage ولا الـ score** — دول مشتقّين. و`save_lead_details` ميقدرش يمسح بيانات معروفة.

---

## التشغيل محليًا

```bash
cp .env.example .env      # واملأ القيم
docker compose up --build
```

بيقوم: Postgres (pgvector) + Redis + migrate + API على `:8000` + Celery worker + beat.

بعد أول تشغيل، نزّل قاعدة المعرفة والـ cadences المبدئية:

```bash
curl -X POST http://localhost:8000/v1/sales/knowledge/seed \
  -H "X-Internal-Api-Key: $INTERNAL_API_KEY"
```

> ⚠️ المستندات المبدئية موصوفة بإنها **للمراجعة** ومفيهاش أي أسعار. لازم فريق المبيعات يقراها ويستبدلها بمحتواه.

جرّب الوكيل من غير أي ربط بـ Meta:

```bash
curl -X POST http://localhost:8000/v1/sales/preview/chat \
  -H "X-Internal-Api-Key: $INTERNAL_API_KEY" -H "Content-Type: application/json" \
  -d '{"session_id":"t1","message":"عايز أدرب فريق 30 موظف، بكام؟"}'
```

## الاختبارات والـ lint

```bash
uv run pytest         # 258 اختبار، مفيهم أي شبكة
uv run ruff check .
```

## الربط بالـ Meta والنشر

خطوات إعداد تطبيق Meta، الـ webhooks، الـ templates، الصلاحيات المطلوبة، وربط الـ LMS — كلها في **[docs/SALES_AGENT.md](docs/SALES_AGENT.md)**.
