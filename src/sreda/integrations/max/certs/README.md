# Сертификаты Минцифры для MAX (platform-api2.max.ru) — #214

`platform-api2.max.ru` отдаёт TLS-сертификат, выпущенный российским
национальным УЦ (Минцифры), которого нет в стандартном хранилище (certifi),
и **НЕ присылает промежуточный Sub** в TLS-цепочке. Поэтому оба корня лежат
здесь и подмешиваются ТОЛЬКО в доверие MAX-клиента
(`integrations/max/client.max_ssl_context`) — НЕ в системное хранилище, НЕ
для прочего outbound.

Сертификаты **публичные** (корни национального УЦ), не секреты.

| Файл | Что | Источник | Отпечаток/идентификатор (сверен #214) |
|---|---|---|---|
| `russian_trusted_root_ca.pem` | Russian Trusted Root CA (self-signed корень) | `gu-st.ru/content/Other/doc/russiantrustedca.pem` (CDN gosuslugi) | SHA-256 `D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31` |
| `russian_trusted_sub_ca_ssl_rsa2024.pem` | Russian Trusted Sub CA SSL RSA 2024 (промежуточный, подписал leaf MAX) | AIA листа: `nuc-cdp.voskhod.ru/cdp/subca_ssl_rsa2024.crt` | SKI `77:3D:D9:39:AF:42:BD:DC:5B:CA:76:EA:EE:FD:CE:3E:61:29:30:5F` = AKI листа `*.max.ru` |

Проверка перед интеграцией (2026-06-24): `GET /me` к platform-api2 с этим
бандлом → `http=200 ssl_verify_result=0`, `openssl verify leaf` → OK.

Отпечаток корня запинён тестом `tests/unit/test_214_max_migration.py` (анти-подмена).
