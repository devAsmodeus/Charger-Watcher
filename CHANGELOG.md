# Changelog

## [0.1.1](https://github.com/devAsmodeus/Charger-Watcher/compare/v0.1.0...v0.1.1) (2026-05-11)


### Documentation

* document CI/release/deploy pipeline ([2054b84](https://github.com/devAsmodeus/Charger-Watcher/commit/2054b8480132fb15db758b008d126782d8fdab77))
* document CI/release/deploy pipeline; scrub host from deploy.yml comments ([f8d7f0b](https://github.com/devAsmodeus/Charger-Watcher/commit/f8d7f0bd0652347be1b686c786a7448bce8af8be))

## 0.1.0 (2026-05-11)


### Features

* **bot:** inline-keyboard onboarding for /start ([18c75b1](https://github.com/devAsmodeus/Charger-Watcher/commit/18c75b1ec243bfb0601504cf2d9a05d516d0575f))
* **bot:** subscribe wizard, notify quota, pre_checkout validation, charge_id persistence, HTML escape ([afc1210](https://github.com/devAsmodeus/Charger-Watcher/commit/afc121085e99783f0ecf36d992b793f0df8ba1ef))
* **compose:** expose gluetun HTTP-proxy on 127.0.0.1:8888 for host Claude Code ([2a9d086](https://github.com/devAsmodeus/Charger-Watcher/commit/2a9d086b562bce9bb0bc81df136d63bcdf61cba1))
* **notifier:** split notification claim from delivery to fix cooldown poisoning ([e03c538](https://github.com/devAsmodeus/Charger-Watcher/commit/e03c538bfce5dce6466328bbe13d1ab88e52bc4d))
* paid subscribe wizard with quota and payments ([9be17e5](https://github.com/devAsmodeus/Charger-Watcher/commit/9be17e598a772192e4c83cffe47b1b4fe08b9878))
* **poller:** connector catalog cache and free-connector enrichment ([b5c2180](https://github.com/devAsmodeus/Charger-Watcher/commit/b5c2180b22f8a6de2c33a08921e411f6b82d55a1))
* **poller:** connector catalog cache and per-event free-connector enrichment ([3359baf](https://github.com/devAsmodeus/Charger-Watcher/commit/3359bafc0f381306b7d916e37b02268c64bdefad))
* **poller:** persist REST diff and SSE baseline to Redis for restart safety ([a464360](https://github.com/devAsmodeus/Charger-Watcher/commit/a4643609f8e58c352f7fbb0b8639153c052fb26e))


### Bug Fixes

* **db:** keep datetime as runtime import in models.py ([320e56a](https://github.com/devAsmodeus/Charger-Watcher/commit/320e56ac705a1b8e596ddc5843a74442ca45e302))


### Reverts

* **compose:** drop unused HTTPPROXY exposure on gluetun ([a2dddc3](https://github.com/devAsmodeus/Charger-Watcher/commit/a2dddc3a8ad0e55366d7c1f2dd56e52a15dfd351))


### Documentation

* add legal documents (ToS, privacy, refund policy, audit) ([68f6738](https://github.com/devAsmodeus/Charger-Watcher/commit/68f6738af94da40e4e3750932d71e43da490f013))
