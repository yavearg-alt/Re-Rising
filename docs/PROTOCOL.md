# SAO Unleash Blading — reverse-engineering notes

Client: `com.bandainamcoent.saoars` 3.7.0 (Cocos2d-x C++, WFS in-house "toybox" engine), `lib/armeabi-v7a/libapp.so`.
The addresses below are virtual addresses in `libapp.so` (Thumb-2 code).

## Crypto (see `tools/saoub_crypto.py`)

| Use | Algorithm | Key / IV | Where |
|---|---|---|---|
| `hashed/*` assets, `*.manifestenc` manifests | AES-256-CBC + PKCS7, then zlib | `ASSET_KEY` / `ASSET_IV` | init 0x1343010, decryption 0x16beed8 |
| Hashed file names | `sha1("66VsXNegycgradYc" + logical_path)` | — | 0x1344a92 |
| Master data fields (FlatBuffers) | XXTEA (last word = length) | `VwG2rFSSrjoBkupe` | 0x10658b4, 0x1838fa4 |
| API request/response bodies (`ENCRYPTION: 1`) | AES-256-CBC + zlib | same key as the assets, IV = `ASSET_IV` | 0x1047ffe / 0x104a3dc |
| Local user data (LevelDB) | AES-256-CBC, MessagePack content | same key, IV = value `encryption/aes_iv` | 0x106858c |

The keys are stored obfuscated: `key[i] = table[idx[i] ^ X] ^ Y`. Images and textures (`.pngenc`, `.pkmenc`…) use a
per-file AES key stored base64 in master table `asset.0` (path → key), with the same IV.

## Master data

- Logical paths `master/divided/<table>.enc` (format `"{}/{}{}"`, 0x1065b7e).
- The version is hard-coded in the client: `88c1fb519dd0883450366f600d36ae41`. The server therefore did not send them.
- 33 tables: area_enemy, asset, base, battle, custom, editor, emblem, enemy, environment, equipment, event, guild, home,
  imagination, item, localization, login_bonus, lottery, map, message, mission, novel, player, pvp, quest, raid_event,
  reward, scenario, season, skill, spine, treasure, tutorial.

## HTTP API

Original servers: `https://api.saoab{,-us,-eu,-ap}.wfs.games`.

Headers sent by the client (0x1048100–0x1049340), all prefixed with `SAOAL-`:
`Content-Type, REQUEST-ID, REQUEST-SEQUENCE, REQUEST-BODY-HASH (16-byte hash in hex), OS, OS-VERSION, DEVICE,
USER-ID, User-Agent, CLIENT-VERSION, CLIENT-VERSION-CODE, ASSET-VERSION, ASSET-HASH, MASTER-DATA-VERSION,
USER-DATA-VERSION, CLIENT-TIMESTAMP, LANGUAGE, ONE-TIME-TOKEN, HAS-ROOT, ENCRYPTION (0/1), LIB-HASH, COUNTRY-CODE,
CURRENCY-CODE, LAST-SIGNATURE, AD-ID, PLATFORM-USER-ID, USER-REGION, USER-COUNTRY, TUTORIAL-USER, SIGNATURE`.
`SIGNATURE` is computed by the client: a private server can ignore it. The private server binds each account to
`PLATFORM-USER-ID` (changes when the game is reinstalled) and `AD-ID` (survives a reinstall); see `api.authorize`.

Headers read from the response (0x1049b54):
`REQUEST-ID, REQUEST-SEQUENCE, REQUEST-BODY-HASH, ONE-TIME-TOKEN, ENCRYPTION, SERVER-TIMESTAMP, SERVER-VERSION,
SERVER-RESPONSE-CODE, RETRY-INTERVAL`. Several are compared (memcmp) with the request's values: echo them back.

Response body: if `ENCRYPTION: 1`, AES-decrypted and decompressed; then the JSON (`application/json`) with `code`,
`serverTime`, `localeServerTime`, `requestEncrypt`, `latestClientVersion`, `remoteLogLevel`, …

## Sample data

- `/data/data` dump of a player's device: Internet Archive, item `gacha-archive`,
  `Online/Sword Art Online - Unleash Blading/com.bandainamcoent.saoars-3.7.0.4728.zip` (4.5 GB), downloaded and unzipped into
  your data folder (`data/` or `$SAOUB_DATA`).
- Its LevelDB holds the full account state (UserInfo, UserPC, UserParty, UserItem, UserMission…) in MessagePack: it is
  the model of the server's `user_data` responses.

## Asset coverage (final manifest `project_android_zip`, version `44bf13dd…`)

64,877 entries in total. The dump and the APK cover 53,943 of them. The 10,934 missing ones break down as follows:
- **9,935 non-English translations** (de, es, fr, kr, tc) never downloaded by this player. English is complete.
- **657 `hashed_XX_N.zip` archives** for grouped downloads: deleted after extraction, not needed (rebuilt by the server).
- **11 duplicates** whose identical content exists under another name.
- **254 unidentified files** (120 MB, category 7, on-demand content): mostly small images and 4 videos of about 28 MB,
  probably localized versions.

28 APK files have an md5 different from the final manifest. They all belong to the non-English language groups: old
versions, no impact for an English client.

Scripts:
- `tools/extract_apk.py`: manifest and master data embedded in the APK.
- `tools/extract_dump.py`: final manifest, final master data (APK + dump) and coverage check.
- `tools/master_strings.py`: extraction of the XXTEA-encrypted strings of the FlatBuffers tables.
