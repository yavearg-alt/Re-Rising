# Catalogue of the game's requests (API)

Extracted from `libapp.so` (route strings + decompilation of the functions that send them, `docs/api_routes.json`).
Columns: route, keys of the request body, server state. Routes missing from `server/api.py` get a generic `code 0`
response without data (the client often believes the action succeeded).

Routes outside the catalogue but known: `start`, `login` (generic, OK), `tutorial/skip/second` (implemented).
Not detected by the automatic extraction (shared functions): `item/exchange`, `lottery/select`, `lottery/pickup/select`,
`lottery/point/exchange`, `friend/follow/remove`, `friend/follower/remove`, `guild/leave`, `guild/invite/reject`,
`pvp/last-reward/receive`, `character/dummy/profile`, `user/initialize`, `user/repair`.

## battle

| Route | Body | Server |
|---|---|---|
| `battle/damage` | `mp`, `normal`, `heal` | ⬜ to do |

## bne

| Route | Body | Server |
|---|---|---|
| `bne/consent/info_get` | — | ⬜ to do |
| `bne/consent/info_post` | `bneConsentInfo` | ⬜ to do |
| `bne/consent/terms` | — | ⬜ to do |

## client

| Route | Body | Server |
|---|---|---|
| `client/log` | `type`, `assetDownloadError`, `errorCode`, `error`, `message`, `failCount` | ✅ implemented |

## equipment

| Route | Body | Server |
|---|---|---|
| `equipment/expand` | `addCount`, `consumeGem` | ✅ implemented |

## event

| Route | Body | Server |
|---|---|---|
| `event/character/reward/receive` | `pcType`, `progress` | ⬜ to do |
| `event/floor/ranking/detail` | `eventId`, `targetUserId` | ⬜ to do |
| `event/ranking/detail` | `eventId`, `targetUserId` | ⬜ to do |
| `event/ranking/list` | `eventId`, `offset`, `limit`, `selfOnly` | ⬜ to do |

## friend

| Route | Body | Server |
|---|---|---|
| `friend/follow/request` | `followUserId` | ⬜ to do |

## gift

| Route | Body | Server |
|---|---|---|
| `gift/receive` | `giftIds` | ✅ implemented |

## guest

| Route | Body | Server |
|---|---|---|
| `guest/search` | `guestUserIds` | ✅ implemented |

## guild

| Route | Body | Server |
|---|---|---|
| `guild/agreement` | — | ✅ implemented |
| `guild/battle/log` | `battleLogId`, `speakerUserId` | ✅ implemented |
| `guild/board/receive` | `guildId`, `thresholdBoardId`, `getCount` | ✅ implemented |
| `guild/board/refresh` | — | ✅ implemented |
| `guild/board/send` | `message`, `guildId` | ✅ implemented |
| `guild/chat/receive` | `thresholdChatId`, `getCount` | ✅ implemented |
| `guild/chat/refresh` | — | ✅ implemented |
| `guild/chat/send` | `message`, `fixedMessageId`, `battleLogType`, `battleLog`, `stampId` | ✅ implemented |
| `guild/force/kick` | `kickedUserId` | ✅ implemented |
| `guild/form` | `name`, `comment`, `atmosphereType`, `iconId`, `isRequiredApproval` | ✅ implemented |
| `guild/info/update` | `name`, `comment`, `atmosphereType`, `iconId`, `isRequiredApproval`, `raidEventDefeatPointBonusHour` | ✅ implemented |
| `guild/invite` | `invitedUserId` | ✅ implemented |
| `guild/invite/accept` | `guildId` | ✅ implemented |
| `guild/invite/cancel` | `invitedUserId` | ✅ implemented |
| `guild/invite/list` | — | ✅ implemented |
| `guild/invite/member/list` | `guildId` | ✅ implemented |
| `guild/invite/recommend/list` | — | ✅ implemented |
| `guild/join/request` | `guildId` | ✅ implemented |
| `guild/join/request/accept` | `requestUserId` | ✅ implemented |
| `guild/join/request/cancel` | `guildId` | ✅ implemented |
| `guild/join/request/list` | `guildId` | ✅ implemented |
| `guild/join/request/reject` | `requestUserId` | ✅ implemented |
| `guild/leave` | — | ✅ implemented |
| `guild/invite/reject` | — | ✅ implemented |
| `guild/board/delete` | — | ✅ implemented |
| `guild/mission/accept` | `guildId`, `index`, `guildMissionId` | ✅ implemented |
| `guild/mission/cancel` | `guildId`, `index`, `guildMissionId` | ✅ implemented |
| `guild/mission/refresh` | `guildId` | ✅ implemented |
| `guild/role/update` | `memberUserId`, `role` | ✅ implemented |
| `guild/search` | `guildId` | ✅ implemented |
| `guild/search/friend` | — | ✅ implemented |
| `guild/search/recommend` | — | ✅ implemented |
| `guild/timeline/receive` | `guildId`, `thresholdTimelineId`, `getCount` | ✅ implemented |
| `guild/top` | — | ✅ implemented |

## imagination

| Route | Body | Server |
|---|---|---|
| `imagination/exchange` | `exchangeInputImaginationStockIds`, `exchangeOutputImaginationId` | ⬜ to do |

## lottery

| Route | Body | Server |
|---|---|---|
| `lottery/draw` | `lotteryPriceId`, `ticketId`, `ticketAmount`, `gemAmount`, `step` | ✅ implemented |
| `lottery/point/convert` | — | ⬜ to do |

## mission

| Route | Body | Server |
|---|---|---|
| `mission/receive` | `missionIds` | ✅ implemented |

## premium-pass

| Route | Body | Server |
|---|---|---|
| `premium-pass/buy` | `premiumPassId` | ⬜ to do |

## pvp

| Route | Body | Server |
|---|---|---|
| `pvp/clear` | `pvpId`, `winFlag`, `elapsedTurn` | ⬜ to do |
| `pvp/matching` | `pvpId`, `reloadFlag`, `expectedConsumingBp` | ⬜ to do |
| `pvp/start` | `pvpId`, `activePartyId`, `npcPartyIndex`, `consumingBp` | ⬜ to do |

## quest

| Route | Body | Server |
|---|---|---|
| `quest/boost-item/purchase` | `boostItemIds` | ⬜ to do |
| `quest/clear` | — | ✅ implemented |
| `quest/continue` | — | ✅ implemented |
| `quest/retire` | `isUserRetire` | ✅ implemented |
| `quest/skip` | — | ✅ implemented |
| `quest/start` | — | ✅ implemented |
| `quest/supporter` | `questId`, `level` | ✅ implemented |

## raid

| Route | Body | Server |
|---|---|---|
| `raid/boss/end` | `eventId`, `bossId`, `isUserRetire` | 🟡 v1 (to validate in game) |
| `raid/boss/info` | `eventId`, `bossId` | 🟡 v1 (to validate in game) |
| `raid/boss/rescue` | `bossId`, `eventId` | 🟡 v1 (to validate in game) |
| `raid/boss/start` | — | 🟡 v1 (to validate in game) |
| `raid/boss/status/update` | `eventId`, `escapeBossIds`, `appearWarningBossIds` | 🟡 v1 (to validate in game) |
| `raid/boss/withdraw` | `eventId`, `bossId` | 🟡 v1 (to validate in game) |
| `raid/guild/ranking/list` | `eventId`, `guildId`, `offset`, `limit` | 🟡 v1 (to validate in game) |
| `raid/guild/ranking/member/list` | `eventId`, `guildId` | 🟡 v1 (to validate in game) |
| `raid/info` | `eventId` | 🟡 v1 (to validate in game) |
| `raid/mvp/elect` | `eventId`, `bossId`, `electUserId` | 🟡 v1 (to validate in game) |
| `raid/rescue/save` | `eventId`, `bossId`, `userIds`, `guildRescueFlag` | 🟡 v1 (to validate in game) |
| `raid/wp/item/recover` | `itemId`, `amount`, `eventId`, `items` | 🟡 v1 (to validate in game) |

## season-point-boost

| Route | Body | Server |
|---|---|---|
| `season-point-boost/buy` | `seasonPointBoostId` | ⬜ to do |

## season-reward

| Route | Body | Server |
|---|---|---|
| `season-reward/receive` | `seasonRewardIds` | ⬜ to do |
| `season-reward/unlock` | `seasonRewardId`, `cost` | ⬜ to do |

## tutorial

| Route | Body | Server |
|---|---|---|
| `tutorial/skip/first` | — | ✅ implemented |

## user

| Route | Body | Server |
|---|---|---|
| `user/eco_mode` | `true`, `false` | ⬜ to do |
| `user/high_resolution` | `true`, `false` | ⬜ to do |
| `user/last_title_world` | — | ⬜ to do |
| `user/migration/confirm` | `migrationUserId` | ⬜ to do |
| `user/pad_reset` | `true`, `bgm_pack`, `se_pack`, `voice_pack`, `low_pack` | ⬜ to do |
| `user/pull` | `tower_error_out_of_period_title`, `tower_error_out_of_period_message`, `event_not_exist`, `HomeVisited`, `SKIP`, `skip_tutorial_2_confirmation`, `CommonUpdated`, `home_dialog_license`, `home_free_term_end_remind`, `season_pt_boost_expired_notice` | ✅ implemented |
| `user/push` | `before`, `after`, `table`, `hash`, `scripts`, `user_data_sync`, `user_data` | ✅ implemented |
| `user/repair` | — | ⬜ to do |
| `user/user_id` | — | ⬜ to do |

## violation

| Route | Body | Server |
|---|---|---|
| `violation/report` | `singularUserId` | ⬜ to do |

- `item/exchange`: ✅ implemented (body `exchangeOutputItemList[{id, count, inputGroupItemId}]`).
