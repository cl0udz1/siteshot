# SiteShot

SiteShot is a single-file Python CLI that discovers and screenshots routes in local web projects using Playwright.

It is designed for quickly reviewing an entire website during development without manually opening every page.

## Features

- Automatically detects common web projects and dev servers
- Supports Windows, Linux, and macOS
- Discovers routes from source files, framework conventions, and rendered links
- Supports Vite, Next.js, SvelteKit, Nuxt, Astro, plain HTML, and basic Django projects
- Captures full-page screenshots
- Best-effort login and signup handling
- Handles redirects, timeouts, broken pages, and unresolved dynamic routes
- Keeps every run in a separate directory without overwriting previous results
- Generates a JSON manifest and HTML screenshot report
- Same-origin crawling by default

## Installation

```bash
pip install playwright
python -m playwright install chromium
```

## Usage

Place `siteshot.py` inside your project and run:

```bash
python siteshot.py
```

SiteShot will try to detect and start the local development server automatically.

If your website is already running locally, pass its localhost URL directly:

```bash
python siteshot.py --url http://localhost:5173
```

or:

```bash
python siteshot.py --url http://127.0.0.1:3000
```

This skips automatic server startup and crawls the already running local site.

Useful options:

```bash
python siteshot.py --headful
python siteshot.py --route /dashboard
python siteshot.py --no-auth
python siteshot.py --max-routes 200
```

## Output

Each run is stored separately:

```text
.siteshot/
├── latest.txt
└── runs/
    └── 20261003-120000/
        ├── screenshots/
        ├── manifest.json
        └── report.html
```

`report.html` provides a simple visual overview of the captured pages.

## Authentication

SiteShot can make best-effort attempts to detect login and signup forms.

Credentials can be provided through CLI options or environment variables. SiteShot avoids submitting unrelated forms and does not store credentials in generated reports.

Authentication involving CAPTCHA, OAuth, magic links, or email verification may require manual setup.

## Configuration

Optional project-specific settings can be added in:

```text
.siteshot.json
```

This can be used for additional routes, authentication credentials, dynamic route values, or a custom base URL.

## Requirements

- Python 3.10+
- Playwright
- Chromium

---

# العربية

SiteShot هي أداة CLI مكتوبة ببايثون في ملف واحد، مهمتها اكتشاف مسارات مشروع الويب المحلي وتصوير صفحاته تلقائيًا باستخدام Playwright.

تفيدك إذا كنت تريد مراجعة صفحات المشروع كاملة بدون فتح كل صفحة وتصويرها يدويًا.

## المميزات

- تحاول اكتشاف مشروع الويب وتشغيل خادم التطوير تلقائيًا
- تعمل على Windows وLinux وmacOS
- تكتشف المسارات من ملفات المشروع، هيكلة الـframework، والروابط الموجودة داخل الصفحات
- تدعم Vite وNext.js وSvelteKit وNuxt وAstro وHTML وبعض مشاريع Django
- تلتقط صورًا كاملة للصفحات
- تتعامل بشكل best-effort مع تسجيل الدخول وإنشاء الحساب
- تستمر حتى لو واجهت صفحات معطلة أو timeouts أو redirects
- لا تستبدل نتائج التشغيلات السابقة
- تنشئ `manifest.json` وتقرير HTML لعرض جميع الصور
- تلتزم بنفس الـorigin أثناء التصفح بشكل افتراضي

## التثبيت

```bash
pip install playwright
python -m playwright install chromium
```

## الاستخدام

ضع `siteshot.py` داخل مجلد المشروع ثم شغّل:

```bash
python siteshot.py
```

ستحاول الأداة اكتشاف خادم التطوير وتشغيله تلقائيًا.

إذا كان موقعك المحلي يعمل بالفعل، يمكنك تمرير رابط localhost مباشرة:

```bash
python siteshot.py --url http://localhost:5173
```

أو:

```bash
python siteshot.py --url http://127.0.0.1:3000
```

في هذه الحالة لن تحاول الأداة تشغيل السيرفر من جديد، وستستخدم الموقع المحلي المفتوح أصلًا.

بعض الخيارات المفيدة:

```bash
python siteshot.py --headful
python siteshot.py --route /dashboard
python siteshot.py --no-auth
python siteshot.py --max-routes 200
```

## النتائج

كل تشغيل يُحفظ في مجلد مستقل:

```text
.siteshot/
├── latest.txt
└── runs/
    └── 20261003-120000/
        ├── screenshots/
        ├── manifest.json
        └── report.html
```

يمكنك فتح `report.html` لمشاهدة الصفحات والصور التي تم التقاطها بشكل سريع.

## تسجيل الدخول

تحاول SiteShot التعرف على نماذج تسجيل الدخول وإنشاء الحساب والتعامل معها بشكل best-effort.

يمكن تمرير بيانات الدخول من خلال خيارات CLI أو متغيرات البيئة. الأداة لا ترسل النماذج العادية ولا تحفظ كلمات المرور داخل التقارير.

الحالات التي تعتمد على CAPTCHA أو OAuth أو magic links أو تفعيل البريد قد تحتاج إعدادًا يدويًا.

## المتطلبات

- Python 3.10+
- Playwright
- Chromium
