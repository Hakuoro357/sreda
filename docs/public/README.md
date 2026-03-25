# Sreda + LangGraph

Эта папка содержит публичный набор архитектурной документации по `Среде` на `LangGraph`.

Базовая идея:
- дать пользователям персональных ассистентов через `Telegram`;
- хранить персональные данные безопасно и изолированно;
- поддержать как SaaS-модель, так и single-tenant deployment profile;
- использовать `LangGraph` как ядро agent runtime;
- держать публичное ядро отдельно от приватных feature modules.

Документация в этой папке описывает:
- продуктовую рамку;
- архитектуру;
- модель данных;
- flow и roadmap;
- базовые security-принципы.

Состав документов:
- [01-product-vision.md](./01-product-vision.md) - цель продукта и базовые требования;
- [02-why-langgraph.md](./02-why-langgraph.md) - почему выбран LangGraph;
- [03-mvp-architecture.md](./03-mvp-architecture.md) - минимальная архитектура первого запуска;
- [04-site-monitoring-pipeline.md](./04-site-monitoring-pipeline.md) - кейс с расписанием, EDS-мониторингом и отправкой в Telegram;
- [05-data-security-and-isolation.md](./05-data-security-and-isolation.md) - хранение данных, шифрование и tenant isolation;
- [06-roadmap.md](./06-roadmap.md) - этапы развития после MVP;
- [07-postgres-schema.md](./07-postgres-schema.md) - состав таблиц PostgreSQL для MVP;
- [08-services-and-processes.md](./08-services-and-processes.md) - какие процессы и сервисы нужны на первом запуске;
- [09-mvp-implementation-spec.md](./09-mvp-implementation-spec.md) - черновое ТЗ на реализацию MVP;
- [10-api-contracts.md](./10-api-contracts.md) - минимальные API-контракты для MVP;
- [11-langgraph-flows.md](./11-langgraph-flows.md) - структура графов и состояния;
- [12-delivery-plan.md](./12-delivery-plan.md) - пошаговый план реализации;
- [16-mvp-backlog.md](./16-mvp-backlog.md) - конкретный backlog на разработку MVP;
- [23-open-core-plugin-model.md](./23-open-core-plugin-model.md) - модель open-core и подключения приватных feature packages;
- [24-privacy-guard.md](./24-privacy-guard.md) - минимальный privacy guard на regex-правилах перед любым вызовом LLM.
- [25-inbound-security-and-secure-storage.md](./25-inbound-security-and-secure-storage.md) - реализованный inbound security layer: secure storage, onboarding-safe webhook persistence и шифрование raw payload.

Текущий базовый выбор:
- язык: `Python`;
- агентный runtime: `LangGraph`;
- API: `FastAPI`;
- основная БД: `PostgreSQL`;
- на MVP без `Redis`;
- канал первого запуска: `Telegram only`;
- первая capability: `eds_monitor`;
- локальные секреты хранятся вне git.
