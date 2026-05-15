# Changelog

## [0.4.0](https://github.com/devAsmodeus/Charger-Watcher/compare/v0.3.0...v0.4.0) (2026-05-15)


### Features

* **bot:** show 'already free' on subscribe, russify /list statuses ([0d5a5f8](https://github.com/devAsmodeus/Charger-Watcher/commit/0d5a5f88bbb7b3c84f368e6e19ca62f4964a5626))
* **logging:** persistent errors.jsonl + docker log rotation ([bff0f89](https://github.com/devAsmodeus/Charger-Watcher/commit/bff0f89c96582f09ca146594375cd9bd4503b3af))


### Bug Fixes

* notification correctness + persistent logs ([2bfd826](https://github.com/devAsmodeus/Charger-Watcher/commit/2bfd826bc117f23c984e2f0d5feb990313855e9b))
* **notifier:** explicit Telegram errors, filter by transitioned, warn on drops ([1e1de5f](https://github.com/devAsmodeus/Charger-Watcher/commit/1e1de5f7bdd3fc8be7245f8df26b12b5dd5470b8))
* **poller:** per-connector SSE transitions, preserve REST baseline on 5xx ([273dc70](https://github.com/devAsmodeus/Charger-Watcher/commit/273dc70c0bedaa17eeb28ef96d2f4b8ec0c023a0))


### Documentation

* drop stale-claim reaper from tech-debt (shipped in 0.2.0) ([0fc3cb2](https://github.com/devAsmodeus/Charger-Watcher/commit/0fc3cb29638aa2b9aed6af535dd26ccfb8c4c857))
* drop two stale CLAUDE.md tech-debt notes ([b95254a](https://github.com/devAsmodeus/Charger-Watcher/commit/b95254a22d083d7d5d0bb955fcb27be641af498b))
* drop two stale tech-debt notes from CLAUDE.md ([e101232](https://github.com/devAsmodeus/Charger-Watcher/commit/e10123228689514e17b9dd026baa9c2bb3aff0d0))

## [0.3.0](https://github.com/devAsmodeus/Charger-Watcher/compare/v0.2.2...v0.3.0) (2026-05-14)


### Features

* show freed connector type in 'Освободилась' push ([424f363](https://github.com/devAsmodeus/Charger-Watcher/commit/424f3635077e78aa8006f4523e44f1c49b9ec773))
* show freed connector type(s) in 'Освободилась' alert ([14774fd](https://github.com/devAsmodeus/Charger-Watcher/commit/14774fd24b28d2308834cf643990ea332ef49002))

## [0.2.2](https://github.com/devAsmodeus/Charger-Watcher/compare/v0.2.1...v0.2.2) (2026-05-13)


### Bug Fixes

* copy audit findings — operator labels, paths, grammar, settings ([06ab925](https://github.com/devAsmodeus/Charger-Watcher/commit/06ab925322a5ebb8475d93a18e6c05a98ddce0d7))

## [0.2.1](https://github.com/devAsmodeus/Charger-Watcher/compare/v0.2.0...v0.2.1) (2026-05-13)


### Bug Fixes

* quiet hours = silent push, not deferred delivery ([1480532](https://github.com/devAsmodeus/Charger-Watcher/commit/148053290ebfae5117aa3105bf7b2e9c1e666c47))

## [0.2.0](https://github.com/devAsmodeus/Charger-Watcher/compare/v0.1.1...v0.2.0) (2026-05-12)


### Features

* expose /privacy and /delete_me as inline buttons in /about ([8490782](https://github.com/devAsmodeus/Charger-Watcher/commit/8490782cea7e9012d73312290340b54fa2f788fa))
* privacy/delete on reply keyboard (4th row) ([5d5c5da](https://github.com/devAsmodeus/Charger-Watcher/commit/5d5c5da7494e070083cd585e764d70921ebc7ad1))
* quiet hours per user (presets + delayed delivery) ([5d47ddc](https://github.com/devAsmodeus/Charger-Watcher/commit/5d47ddc273b0ec342ded5c7c76f2c2cf40e06f17))
* referral program with reverse-on-refund ([5cbbc89](https://github.com/devAsmodeus/Charger-Watcher/commit/5cbbc895a614d64e7e0f0722ce15d322d8c3ff05))


### Bug Fixes

* stale-claim reaper for notification_log ([4f42b97](https://github.com/devAsmodeus/Charger-Watcher/commit/4f42b97c89ef31111beffe283dcb0dc16895c954))


### Documentation

* mini-app map plan synthesized from parallel research ([694c46a](https://github.com/devAsmodeus/Charger-Watcher/commit/694c46a47674bbb24af6e6a7d1fd80182fb9ea0c))

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
