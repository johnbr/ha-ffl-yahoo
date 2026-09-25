# Changelog

## [0.18.1](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.18.0...v0.18.1) (2026-09-25)


### Bug Fixes

* hold the games feed too, and stop a blank slate slowing its own recovery ([#35](https://github.com/johnbr/ha-ffl-yahoo/issues/35)) ([198f7f0](https://github.com/johnbr/ha-ffl-yahoo/commit/198f7f01b15b75c1ee73a2d7ec67e85699444250))

## [0.18.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.17.2...v0.18.0) (2026-09-25)


### Features

* title the NFL card "NFL" rather than "NFL Games" ([#33](https://github.com/johnbr/ha-ffl-yahoo/issues/33)) ([c415b56](https://github.com/johnbr/ha-ffl-yahoo/commit/c415b56a23d6f903a2ecb316015d330fd45a36df))

## [0.17.2](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.17.1...v0.17.2) (2026-09-25)


### Bug Fixes

* name collisions on the NFL card, and a dropped stat feed scoring the league zero ([#31](https://github.com/johnbr/ha-ffl-yahoo/issues/31)) ([0c33968](https://github.com/johnbr/ha-ffl-yahoo/commit/0c339681df285abebda5af643737ff83b77a7aaf))

## [0.17.1](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.17.0...v0.17.1) (2026-09-22)


### Bug Fixes

* fold the pieces of one play into one scoring row ([#29](https://github.com/johnbr/ha-ffl-yahoo/issues/29)) ([dc7c3a8](https://github.com/johnbr/ha-ffl-yahoo/commit/dc7c3a88711b71d07e7737a24b1739ad3bc8e18a))

## [0.17.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.16.0...v0.17.0) (2026-09-22)


### Features

* shorten the names on the NFL card's last-play line ([#27](https://github.com/johnbr/ha-ffl-yahoo/issues/27)) ([ebff958](https://github.com/johnbr/ha-ffl-yahoo/commit/ebff95808b4981c6054d242b3514cdb2e5d68a99))

## [0.16.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.15.1...v0.16.0) (2026-09-20)


### Features

* rebuild the lineup block, trim the stat line, and handle a delayed NFL game ([#25](https://github.com/johnbr/ha-ffl-yahoo/issues/25)) ([30f1bdd](https://github.com/johnbr/ha-ffl-yahoo/commit/30f1bdd62a93ce50f4741d2350e25a871e5916b5))

## [0.15.1](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.15.0...v0.15.1) (2026-09-20)


### Bug Fixes

* stop missing downs on the NFL card; lineup colour, halftime and play-line polish ([#23](https://github.com/johnbr/ha-ffl-yahoo/issues/23)) ([d87ef3a](https://github.com/johnbr/ha-ffl-yahoo/commit/d87ef3aafde05f5a8d655a3cfabc4f52b9cde50e))

## [0.15.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.14.2...v0.15.0) (2026-09-19)


### Features

* show a player's final score in gold ([#21](https://github.com/johnbr/ha-ffl-yahoo/issues/21)) ([4869ae2](https://github.com/johnbr/ha-ffl-yahoo/commit/4869ae2d9b39e23d8c040e1ad8794d9c14a2a912))

## [0.14.2](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.14.1...v0.14.2) (2026-09-19)


### Bug Fixes

* whiten the expanded lineup's quiet lines and weight the real scores ([#19](https://github.com/johnbr/ha-ffl-yahoo/issues/19)) ([07bbe3f](https://github.com/johnbr/ha-ffl-yahoo/commit/07bbe3f4eba489da4f96f3a770e97ff753700d5a))

## [0.14.1](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.14.0...v0.14.1) (2026-09-18)


### Bug Fixes

* settle games the relay forgets overnight; brighten the expanded lineup ([#17](https://github.com/johnbr/ha-ffl-yahoo/issues/17)) ([ecacb4a](https://github.com/johnbr/ha-ffl-yahoo/commit/ecacb4a9f4dd12ca099c08cca816a80db63af2d0))

## [0.14.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.13.0...v0.14.0) (2026-09-18)


### Features

* down & distance on plays, kickoff times on rosters, live-only NFL view ([e5e4ab4](https://github.com/johnbr/ha-ffl-yahoo/commit/e5e4ab40bf07c74837145a3cf9c9e848639fd73b))
* drop AM/PM from kickoff times except between 11 PM and 7 AM ([1f74698](https://github.com/johnbr/ha-ffl-yahoo/commit/1f7469836449ab3d4a7f800637886e8e19e2c50a))
* fold scheduled NFL games until the day they are played ([ae6afc5](https://github.com/johnbr/ha-ffl-yahoo/commit/ae6afc5ccad2ebf1aa9c33188c9936711fde0feb))


### Bug Fixes

* stop missing plays — poll play feeds conditionally, fix week rollover ([4b3c57c](https://github.com/johnbr/ha-ffl-yahoo/commit/4b3c57cf22be4e9822d9d683d93924391842acb4))

## [0.13.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.12.0...v0.13.0) (2026-09-15)


### Features

* colour the winning score gold once a result is in ([5e62727](https://github.com/johnbr/ha-ffl-yahoo/commit/5e6272797d6cfb9fd090aaabf6e2980d37af2877))
* show only the next game day by default and keep both NFL folds shut ([6f40f0d](https://github.com/johnbr/ha-ffl-yahoo/commit/6f40f0d692f612f13c4c09069912f8b2365b4d53))

## [0.12.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.11.1...v0.12.0) (2026-09-15)


### Features

* leave the tackler out of play descriptions ([470b03a](https://github.com/johnbr/ha-ffl-yahoo/commit/470b03acd4fc3dc946904ff67a6ac795497fad3a))

## [0.11.1](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.11.0...v0.11.1) (2026-09-15)


### Bug Fixes

* wrap the NFL card's last play instead of truncating it ([9261963](https://github.com/johnbr/ha-ffl-yahoo/commit/9261963e9d4203f9f4e08ef9f48805ced3e0d13b))

## [0.11.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.10.0...v0.11.0) (2026-09-15)


### Features

* draw each live NFL game's field as a bar under the clubs ([b2dcc21](https://github.com/johnbr/ha-ffl-yahoo/commit/b2dcc215f6d5bd396c92a45f131cda0ff8aded00))
* wrap the row's play line and break it between player and result ([26e1399](https://github.com/johnbr/ha-ffl-yahoo/commit/26e139942f4b18bf7974a87dcebff6f56d092af8))


### Bug Fixes

* keep the projections under the scores and the live badge centred ([f84a307](https://github.com/johnbr/ha-ffl-yahoo/commit/f84a3077781d67b3077fa19d815e60f41303260d))
* wrap the lineup stat line instead of truncating it ([dd6eb02](https://github.com/johnbr/ha-ffl-yahoo/commit/dd6eb021cfe7b14708973473df2179b240fe5e5f))

## [0.10.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.9.0...v0.10.0) (2026-09-15)


### Features

* wrap long team names and set the matchup row in smaller type ([674ea4c](https://github.com/johnbr/ha-ffl-yahoo/commit/674ea4cfff02b65a154090ed821b59074727e768))


### Bug Fixes

* make the collapsed NFL rows legible on a dark theme ([33d799a](https://github.com/johnbr/ha-ffl-yahoo/commit/33d799a3098ae5f59f3ab63398080d69d2c140e1))

## [0.9.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.8.0...v0.9.0) (2026-09-14)


### Features

* add an NFL games card showing the real slate ([1244700](https://github.com/johnbr/ha-ffl-yahoo/commit/124470030d2ad98fe4e5212e32a07e014da52587))
* fold finished NFL games behind a toggle at the foot of the card ([ae214c7](https://github.com/johnbr/ha-ffl-yahoo/commit/ae214c7834ef3124bb16b7fca9130967b13bcba1))
* show each NFL game's last play and the ball's yard line ([d627dcb](https://github.com/johnbr/ha-ffl-yahoo/commit/d627dcbe22538783c6bf59c7595c2e8b2b39b826))


### Bug Fixes

* refresh NFL last plays when a play runs, not when a TTL lapses ([553ea12](https://github.com/johnbr/ha-ffl-yahoo/commit/553ea124fa95f78352be8ebc16c18c301603eacc))

## [0.8.0](https://github.com/johnbr/ha-ffl-yahoo/compare/v0.7.0...v0.8.0) (2026-09-13)


### Features

* move the live projections onto the play line's row ([3669e33](https://github.com/johnbr/ha-ffl-yahoo/commit/3669e33972714f5c9a5139f07386b2737fdc4911))
* put live projections under the scores and drop the expand arrow ([383704c](https://github.com/johnbr/ha-ffl-yahoo/commit/383704c1d3033fdc8d6d40ac53cd0844aa76984f))


### Bug Fixes

* stop filing interceptions and other negative plays as stat corrections ([f180a96](https://github.com/johnbr/ha-ffl-yahoo/commit/f180a96f9fc32e1cd916763b4ef905237cd0c6ea))

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
