# دليل تشغيل وكيل المبيعات

الدليل ده بيغطي الإعداد من الصفر: تطبيق Meta، الـ webhooks، القوالب، ربط الـ LMS، والتشغيل اليومي.

---

## 1. المتطلبات قبل أي حاجة

| المتطلب | ليه |
|---|---|
| Meta Business Account موثّق | شرط لـ WhatsApp Cloud API ولـ Lead Ads |
| رقم واتساب مخصّص للأعمال | **مايكونش مستخدم على تطبيق WhatsApp العادي** — الرقم بيتنقل للـ Cloud API |
| صفحة فيسبوك | للـ Messenger و comment-to-DM و Lead Ads |
| Azure OpenAI | deployment للـ chat (`gpt-5-mini` أو أعلى) وdeployment للـ embeddings (`text-embedding-3-small`) |
| PostgreSQL 16 + pgvector | قاعدة معرفة المبيعات |
| Redis | Celery broker |

---

## 2. تطبيق Meta

### 2.1 إنشاء التطبيق
1. [developers.facebook.com](https://developers.facebook.com) → **My Apps** → **Create App** → نوع **Business**.
2. من **App Settings → Basic** خُد **App Secret** → `META_APP_SECRET`.
   > الـ webhooks بترفض كل الطلبات لو المتغير ده فاضي. ده مقصود: نقطة النهاية دي بتقدر تخلّي الوكيل يبعت رسايل، فالفشل لازم يكون مُغلق.

### 2.2 WhatsApp
1. أضف product **WhatsApp** → **API Setup**.
2. خُد **Phone number ID** → `WHATSAPP_PHONE_NUMBER_ID`.
3. اعمل **System User** بصلاحية `whatsapp_business_messaging` + `whatsapp_business_management` وخُد توكن دائم → `WHATSAPP_ACCESS_TOKEN`.
   > التوكن المؤقت في صفحة الـ API Setup بينتهي بعد 24 ساعة. مايستخدمش في الإنتاج.
4. اختار أي نص عشوائي طويل كـ `WHATSAPP_VERIFY_TOKEN`.
5. **Configuration → Webhook**: الرابط `https://<your-host>/v1/sales/webhooks/whatsapp` والـ verify token اللي اخترته. اشترك في الحقل **messages**.

### 2.3 Messenger + الصفحة
1. أضف product **Messenger** → **Settings**.
2. اربط الصفحة وخُد **Page Access Token** → `MESSENGER_PAGE_ACCESS_TOKEN`، و**Page ID** → `MESSENGER_PAGE_ID`.
3. `MESSENGER_VERIFY_TOKEN` = نص عشوائي تاني.
4. Webhook: `https://<your-host>/v1/sales/webhooks/messenger`، واشترك في:
   - `messages`, `messaging_postbacks` → المحادثات المباشرة
   - `feed` → **التعليقات** (ده اللي بيشغّل comment-to-DM)
   - `leadgen` → **فورمات Lead Ads**
5. الصلاحيات المطلوبة للمراجعة (App Review): `pages_messaging`, `pages_manage_metadata`, `pages_read_engagement`, `leads_retrieval`.

---

## 3. قوالب واتساب (Templates)

برّا نافذة الـ 24 ساعة، **القالب المعتمد هو الطريقة الوحيدة** للتواصل. من غير قوالب، الـ cadences هتشتغل جوه النافذة بس، وleads الفورمات هتستنى موظف بشري.

اعمل قالبين من **WhatsApp Manager → Message Templates**:

**أ) أول تواصل بعد فورم** (category: `MARKETING` أو `UTILITY` حسب الصياغة)
```
اهلا {{1}} 👋 وصلنا طلبك من {{2}}.
ينفع أساعدك وأعرف إيه اللي بتدور عليه بالظبط؟
```
حُط اسمه في `SALES_LEAD_FORM_TEMPLATE_NAME`.

**ب) إعادة تواصل في الـ cadence**
```
اهلا {{1}}، أنا من فريق {{2}}. لسه معاك سؤال محتاج إجابة؟
```
حُط اسمه في `SALES_REENGAGE_TEMPLATE_NAME`.

الوكيل بيبعت المتغيرين بالترتيب: **(1)** الاسم الأول (أو "حضرتك")، **(2)** اسم الشركة من `SALES_COMPANY_NAME`.

> سيب المتغيرين فاضيين في `.env` لحد ما Meta توافق على القوالب. اسم قالب غير معتمد بيفشل في صمت وقت الإرسال — والخدمة بتسجّل الرفض بـ `TEMPLATE_REQUIRED_BUT_MISSING` بدل ما تبعت نص مخالف.

---

## 4. ربط الـ LMS

### 4.1 المتغيرات في الوكيل
```
LMS_API_BASE_URL=https://api.example.com/api/v1
LMS_INTERNAL_API_KEY=<نفس القيمة اللي في الـ LMS>
SALES_WEB_BASE_URL=https://app.example.com
```

الوكيل بيقرأ اتنين endpoints:
- `GET /catalog/courses` — عام، للكتالوج.
- `GET /sales/internal/pricing` — محمي بـ `X-Internal-Api-Key`، للباقات.

### 4.2 المتغيرات في الـ LMS (NestJS)
```
SALES_AGENT_URL=https://sales-agent.example.com
SALES_AGENT_INTERNAL_API_KEY=<نفس INTERNAL_API_KEY في الوكيل>
SALES_INTERNAL_API_KEY=<نفس LMS_INTERNAL_API_KEY في الوكيل>
```

⚠️ **مفتاحين مختلفين، وكل واحد في اتجاه:**
- `INTERNAL_API_KEY` (في الوكيل) = `SALES_AGENT_INTERNAL_API_KEY` (في الـ LMS) → الـ LMS بينده الوكيل.
- `LMS_INTERNAL_API_KEY` (في الوكيل) = `SALES_INTERNAL_API_KEY` (في الـ LMS) → الوكيل بينده الـ LMS.

خلطهم يخلّي اتجاه واحد يشتغل والتاني يرجّع 401.

---

## 5. أول تشغيل

```bash
alembic upgrade head        # أو: docker compose up migrate

curl -X POST https://<host>/v1/sales/knowledge/seed \
  -H "X-Internal-Api-Key: $INTERNAL_API_KEY"
```

اتأكد إن كل حاجة موصولة:
```bash
curl https://<host>/v1/ready -H "X-Internal-Api-Key: $INTERNAL_API_KEY"
```
بيرجّع كل تكامل و`true`/`false` — أسرع طريقة تعرف "ليه مش بيرد على واتساب؟".

### قاعدة المعرفة
المستندات المبدئية **مافيهاش أرقام ولا أسعار** بشكل مقصود، وكل واحد فيها موصوف بـ `[محتوى مبدئي — للمراجعة]`. استبدلها بمحتواكم:

```bash
curl -X POST https://<host>/v1/sales/knowledge \
  -H "X-Internal-Api-Key: $KEY" -H "Content-Type: application/json" \
  -d '{
    "title": "باقات الشركات 2026",
    "doc_type": "PRICING",
    "audience": "B2B",
    "locale": "ar",
    "content": "..."
  }'
```

`doc_type`: `PRODUCT` | `PRICING` | `FAQ` | `OBJECTION` | `CASE_STUDY` | `POLICY` | `COURSE`
`audience`: `ALL` | `B2B` | `B2C` — والـ retrieval بيوسّع مايضيّقش (عميل B2B بيشوف محتوى B2B **و** ALL).

نفس المحتوى بالحرف = مايتكرّرش (de-dup على hash المحتوى)، فإعادة الرفع آمنة.

---

## 6. سياسة الرسائل بالتفصيل

`app/sales/policy.py` بيرجّع واحد من أربعة:

| القرار | معناه |
|---|---|
| `ALLOW_FREEFORM` | نص عادي مسموح (النافذة مفتوحة) |
| `ALLOW_TEMPLATE` | قالب معتمد بس |
| `DEFER` | مسموح بس مش دلوقتي — `retry_at` بيقول امتى |
| `DENY` | ممنوع نهائيًا |

الترتيب متعمّد: الممنوعات المطلقة قبل التوقيتات، فعميل عمل opt-out **مايتأجّلش** أبدًا، والسبب المسجّل دايمًا هو الأعمق.

أسباب الـ `DENY`: `OPTED_OUT`, `HUMAN_TAKEOVER`, `TERMINAL_STAGE`, `NO_CONSENT`, `TOTAL_CAP`, `MESSENGER_WINDOW_CLOSED`.
أسباب الـ `DEFER`: `DAILY_CAP`, `QUIET_HOURS`.

### التحكم
```
SALES_QUIET_HOURS_START=21
SALES_QUIET_HOURS_END=9
SALES_DEFAULT_TIMEZONE=Africa/Cairo
SALES_MAX_OUTBOUND_PER_LEAD_PER_DAY=1
SALES_MAX_OUTBOUND_PER_LEAD_TOTAL=5
SALES_AGENT_ENABLED=true      # false = مفتاح إيقاف فوري
```

`SALES_AGENT_ENABLED=false` بيوقّف كل المعالجة والإرسال، والـ webhooks تفضل ترجّع 200 (لو رجّعت خطأ، Meta بتعيد المحاولة وبعدين توقف الاشتراك).

---

## 7. الـ Cadences

خطوات الـ cadence بتتخزّن كـ JSONB في `sales_sequences`. كل خطوة:

```json
{
  "delay_hours": 24,
  "channel": "WHATSAPP",
  "goal": "restart the conversation",
  "body": "اهلا {{name}}، ..."
}
```

المتغيرات المتاحة: `{{name}}` (الاسم الأول، وبيرجع لـ "حضرتك"/"there" لو مش معروف)، `{{company}}`, `{{product}}`, `{{agent}}`.

**اللي بيوقّف الـ cadence فورًا:** العميل رد (`LEAD_REPLIED`)، عمل opt-out، موظف بشري خد المحادثة، أو وصل لمرحلة الهدف (QUALIFIED / DEMO_BOOKED / PROPOSAL_SENT / WON).

الخطوة بتتبعت نص عادي لو النافذة مفتوحة، وبقالب لو مقفولة — فالقالب مايتصرفش (وميتكلّفش) إلا لما يكون ضروري.

تسجيل عميل في cadence:
```bash
curl -X POST https://<host>/v1/sales/leads/<lead_id>/sequences \
  -H "X-Internal-Api-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"sequence_name": "b2b-followup"}'
```

---

## 8. التشغيل اليومي

### المكوّنات
| العملية | الأمر | ملاحظة |
|---|---|---|
| API | `uvicorn app.main:app` | يقدر يعمل scale أفقي |
| Worker | `celery -A app.worker.celery_app worker` | يقدر يعمل scale أفقي |
| Beat | `celery -A app.worker.celery_app beat` | **نسخة واحدة بس** — اتنين beat = كل رسالة مجدولة تتبعت مرتين |

### المراقبة
- `GET /v1/sales/stats` — leads بالمرحلة والقناة، رسايل داخلة/خارجة آخر 7 أيام، الطابور المعلّق، **الرفضات** آخر 7 أيام، وأعلى الحملات.
- ارتفاع مفاجئ في `outbound_skipped_last_7d` = فيه إعداد غلط (قالب مش معتمد، أو leads بغير أرقام).
- كل عميل عنده timeline كامل في `GET /v1/sales/leads/{id}` — كل تغيير حالة، كل رفض، وكل tool الوكيل استخدمه.

### تفريغ الطابور فورًا بعد إصلاح إعداد
```bash
curl -X POST https://<host>/v1/sales/outbound/drain \
  -H "X-Internal-Api-Key: $KEY" -H "Content-Type: application/json" -d '{"limit": 100}'
```

### تحويل محادثة لموظف بشري
```bash
curl -X POST https://<host>/v1/sales/leads/<lead_id>/messages \
  -H "X-Internal-Api-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"text": "اهلا، أنا أحمد من الفريق...", "actor": "<firebase-uid>"}'
```
افتراضيًا ده بيعمل **takeover**: الوكيل يسكت على العميل ده خلاص، فمايحصلش إن موظف وموديل يجاوبوا نفس السؤال.

لرجوع الوكيل: `PATCH /v1/sales/leads/<id>` بـ `{"human_takeover": false}`.

---

## 9. حدود معروفة

- **مفيش وسائط.** صور وفويس نوتس بتتسجّل كـ `[image]` / `[audio]` ومابتتحلّلش. لو العميل بعت صوت، الوكيل بيرد على النص المكتوب حواليها بس.
- **ماسنجر مافيهوش متابعة برّا 24 ساعة.** مقصود — مفيش طريقة متوافقة، فالوكيل بيستنى.
- **الـ quotes للفرق استرشادية.** `SALES_VOLUME_DISCOUNT_BANDS` بيعرّف خصومات معلنة بس؛ أي تفاوض بيتحوّل لموظف.
- **Tenant واحد افتراضيًا.** الجداول كلها فيها `tenant_id` ومُفهرسة عليه، لكن الـ webhooks بتستخدم `DEFAULT_TENANT_ID`؛ multi-tenant حقيقي محتاج ربط رقم/صفحة بكل tenant.
- **تقدير التوكنز.** لو `tiktoken` مش قادر يحمّل قاموسه (شبكة مقيّدة)، بيرجع لتقدير بالحروف مع warning في اللوج. الـ Dockerfile بيخبّي القاموس جوه الصورة فالإنتاج مايوصلش للحالة دي.

---

## 10. استكشاف الأخطاء

| العَرَض | السبب المحتمل |
|---|---|
| Webhook بيرجّع 403 | `META_APP_SECRET` غلط أو فاضي، أو الـ verify token مش مطابق |
| مفيش رد على واتساب | شوف `/v1/ready` → `whatsapp: false` يعني التوكن أو الـ phone id ناقص |
| الـ leads بتتسجّل بس مفيش رد | `SALES_AGENT_ENABLED=false`، أو الـ lead عليه `human_takeover` |
| Cadence مش بتبعت | القالب مش معتمد → شوف `skip_reason` في `sales_outbound_messages` |
| الوكيل بيقول "هأكد وأرجعلك" على كل حاجة | قاعدة المعرفة فاضية أو غير مرفوعة — شغّل `/knowledge/seed` وارفع محتواكم |
| Leads متكررة لنفس الشخص | طبيعي لو التليفون والإيميل مش معروفين — الربط بيحصل أول ما يظهر أي واحد منهم |
