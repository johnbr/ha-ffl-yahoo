# Changelog

## [0.7.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.6.0...v0.7.0) (2026-09-13)


### Features

* collapse each matchup row to one line and move projections behind it ([1324a39](https://github.com/johnbr/ha-ffl-yahoo/commit/1324a39ced434b22c5d7c700342329cc68d392d7))
* show the compact player-side stat line on each matchup row ([0222a04](https://github.com/johnbr/ha-ffl-yahoo/commit/0222a04f01d17442d1882787e6b96dcb8cc50b30))


### Bug Fixes

* keep the chevron on the play's row when the away side scored ([5f4d806](https://github.com/johnbr/ha-ffl-yahoo/commit/5f4d806afb3146581366744dfef6eeb48575d8c2))
* pin a play revision to its own play instead of re-picking the newest ([3fe992a](https://github.com/johnbr/ha-ffl-yahoo/commit/3fe992ac6b55fdc11c1c623bc8584dc4d170d16c))


### Performance Improvements

* halve live poll latency and stop serialising the relay fetches ([50ed13c](https://github.com/johnbr/ha-ffl-yahoo/commit/50ed13c7947d1683362f09e6a6ad1b02cf356c6f))

## [0.6.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.5.0...v0.6.0) (2026-09-11)


### Features

* read Yahoo's GameChannel relay tier for live projections and plays ([57abaec](https://github.com/johnbr/ha-ffl-yahoo/commit/57abaec79b3070acebd1d11d108a2796bbe00b0e))
* rebuild the scoreboard card around live scoring and inline panels ([05dc44f](https://github.com/johnbr/ha-ffl-yahoo/commit/05dc44f956e41f8116836d1decc7fbf9c832a0d0))

## [0.5.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.4.0...v0.5.0) (2026-08-03)


### Features

* build both cards, the roster popup and the play history ([977bed0](https://github.com/johnbr/ha-ffl-yahoo/commit/977bed0944afed996236d4ab830c8646e706054a))
* make the integration work end to end on the public web source ([5defa3a](https://github.com/johnbr/ha-ffl-yahoo/commit/5defa3afafc844530f9dab33ec4d716ec92c54bb))
* parse Yahoo's public web tier ([d5096d4](https://github.com/johnbr/ha-ffl-yahoo/commit/d5096d45797ac63f6cb42ab0f6189603720dc971))

## [0.4.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.3.0...v0.4.0) (2026-08-01)


### Features

* add demo mode ([5548809](https://github.com/johnbr/ha-ffl-yahoo/commit/55488098d6fb96383b63196457102b79d3f47685))

## [0.3.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.2.1...v0.3.0) (2026-08-01)


### Features

* add ESPN scoring-play parser and player matcher ([f96e0a3](https://github.com/johnbr/ha-ffl-yahoo/commit/f96e0a39dc147ef34575a8d3616ee2b48a342ecc))
* add the scoring-play engine ([2a5a545](https://github.com/johnbr/ha-ffl-yahoo/commit/2a5a545ca890aeb1f897de80952905435320620c))

## [0.2.1](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.2.0...v0.2.1) (2026-08-01)


### Bug Fixes

* drop the bogus card editor element ([5f440ed](https://github.com/johnbr/ha-ffl-yahoo/commit/5f440edd14cd832c670ccb3c1babb3d799f7f8b0))

## [0.2.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.1.0...v0.2.0) (2026-08-01)


### Features

* scaffold integration, cards and HACS metadata ([b5416b3](https://github.com/johnbr/ha-ffl-yahoo/commit/b5416b376ddafbcab034c895afccb0a75a61ea15))


### Bug Fixes

* drop the URL from the config-flow description ([54f0211](https://github.com/johnbr/ha-ffl-yahoo/commit/54f0211a4093c24b8d483cf83b819b9da777de14))
