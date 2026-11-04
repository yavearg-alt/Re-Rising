# Changelog

## 2026-11-04 — Bug fixes from the beta reports

- **INC gauge kept between battles** (a battle could start at 200 %, Ordinal included): `UserAnotherForce.gauge` is
  now emptied at quest start, quest clear / retire, Ordinal start and clear; Ordinal clear also heals the party.
- **Daily-limited events (Vigil Key, Growth Plant, EXP Blessing: 5 runs a day) stayed at 5/5 runs left**:
  `UserEventProgress.playCount` is counted at each clear and starts again each game day (04:00 JST);
  `questProgress.playCountOfDay` too.
- **Discord bot: equipment with no stats** (NPC / enemy weapons such as FD Kirito's greatsword, the "(3D)" models) is
  no longer offered nor claimable: it came as an empty outline that crashed the blacksmith.
- **Opening the guild invitations crashed the game**: the inviting guilds were missing from `UserGuild`.
- **Missions never counted**: Incarnate skills used, quests cleared with an EXP / Shia / item boost, players
  followed, equipment inventory expanded.
- **"+50 AP" mission reward gave less**: it now adds to the current AP (with the recovery since the last update).
- **Ordinal consecutive-win bonus**: the rank's own bonus from pvp.bin (30 at rank 1 ... 160 at rank 9) from the 2nd
  win in a row.
- **Freeze when buying a boost without enough Col**: the server now always answers with the item table.
- **Equipment Creation Event Tutorial mission stuck at 0/1**: the tutorial skip now clears its quest, and login
  repairs the accounts skipped before.
- **Free scout always drew 1** whatever number was picked; **choosing a pick-up doubled the first step's price**.
- **Scout banners always "New"** after a restart (the device's read states are kept now).
- **Scout "Details" / "Rates" opened the news**: they show the scout's rates now.
- **Iskahn / Tales of ARISE Kirito Imaginarium never received** (381xxxxxx ids); the ones already in the item list
  move to the Imaginarium stock at the next login.
- **Event point seasons** ("Unleash Blading"): event quests now give their event season points.
- **One-time campaign login bonuses** giving an illustration or a character no longer come back every day.
- **Refused draws / purchases / exchanges** (not enough Diamond Cubes, tickets or points, limit reached) answer with
  the current tables instead of an empty answer the game kept waiting on, and the reason is logged (`REFUSED`).
- **Limit break 4** gives the character's own Imaginarium (once; at login for characters already there).
- **`imagination/exchange`**: 5 Imaginarium for one character Imaginarium (the route had no handler).
- New: **`update_from_git.bat`** updates a git clone of this repository (refuses to run over local edits, refreshes
  the Python packages when `requirements.txt` changed).
- New server caches built at the first start: `master_event_play_limits.json`, `master_equipment_statless.json`,
  `master_pvp_rank_rows2.json`, `master_event_seasons.json`, `master_login_bonus_once.json`,
  `master_pc_imaginations.json`, `master_imagination_exchangeable.json`.
