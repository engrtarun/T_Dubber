# DIRECT LINK → TELEGRAM UPLOAD — Approach Book

**Goal:** fast + quality upload. Video link se Telegram channel tak, apne system me
media store kiye bina.

**Status:** plan + P0 landed. **Kuch numbers ab MEASURED hain, kuch abhi bhi
predicted.** Neeche har number ke saath `MEASURED` / `DERIVED` / `PREDICTED`
likha hai. Measurement ka tareeqa `bench_p0.py` hai, results `bench_p0_results.json`.

**Repo facts verified:** telethon 1.45.0, gotd v0.162.0, `CHUNK_SIZE = 1900 MiB`.

> ### ⚠️ Is document ko padhne se pehle ye padho
>
> **1. Go fast-path aaj ZINDA NAHI hai.** `tgup.session` file mojood hai par
> unauthorized hai. Har upload pe tgup fail karta hai aur Telethon par girta
> hai. Measured proof neeche Section 9b me hai.
>
> **2. Isliye `TGUP.md` ka "2.06 MB/s single-stream" figure aaj applicable nahi
> hai.** Aaj is machine pe measured **1.00 MB/s**. Wo shayad 5 Oct ko dusri
> connection pe tha.
>
> **3. 🔴 Mera top recommendation (A4 = N connections) Telegram ke OFFICIAL
> docs ke against hai.** `core.telegram.org/api/optimisation` kehta hai:
> *"When uploading data to a server **one connection is enough to achieve the
> best results**."* Neeche Section 10 me poora discussion hai. Matlab
> **2.7x/3.7x abhi bhi `PREDICTED` hai, aur wo galat bhi ho sakta hai.**
>
> **4. Neeche ka "2.7x / 3.7x" abhi bhi PREDICTED hai** — wo N-connection
> concurrency gain par tika hai, wo gain aaj in effect nahi hai, aur uske
> against official guidance hai. P1 adhoora pada hai (Section 9b).

---

## 1. TL;DR

| | |
|---|---|
| **Aaj ka flow** | link → disk → copy → disk → upload → Kaggle dataset → Kaggle se wapas |
| **Aaj ka problem** | download aur upload serialized (no overlap) + source video hamare uplink se **3 baar** jaata hai + **Go fast-path mar chuka hai** |
| **Kitne approaches** | **5** viable (A1–A5) + 1 rejected (A6) |
| **Meri choice** | **A4 = `tgup --url` (ranged parallel pull + N MTProto connections)**, aur page-URLs ke liye **A7 = tail-follow growing file** |
| **Extra bada kaam** | worker ko media Kaggle dataset se nahi, **channel se** (`tgup fetch` already exists) |
| **Stack change** | koi naya language nahi. Go (`tgup`) me `ReaderAt` over HTTP Range + Telethon ke 20 MB wale case me |
| **P0 (landed)** | Tier 0 dedup, content-keyed resume, dataset se media hata |
| **P0 measured saving** | duplicate 200 MB upload: **199.8 s → 0.00 s**. Crash-resume+move, 40 MB: **65.6 s → 30.6 s**. `MEASURED` |
| **A4 (`tgup --url`) — LANDED 2026-10-09** | zero-disk direct upload chalu hai. `MEASURED` — URL aur file ke digests byte-identical, 1 ranged GET, 0 bytes disk. Neeche Section 9.5 dekho |
| **Cancelled** | **P1 (concurrency gain) — Tarun ne 17:40 par cancel kar diya** ("P1 bench cancel samjho"). Wo claim ab PREDICTED hi rahega, is doc me bhi |
| **Blocked** | ~~tgup session unauthorized~~ — **sirf OTP wala rasta tha.** `--url` ka dry-run **bina session** poora plan + digests deta hai |

> **TL;DR ka nawaqs (2026-10-09, NEEL):** is doc ka "Stack change" line ab
> **plan nahi, code** hai — `source.go` me `ReaderAt`-style `source` interface
> (file + http). Jo bhi is doc ko aage padhe: Section 9.5 padho pehle, wo
> batata hai ki kya **tatha-kya** bana aur kya **abhi bhi** nahi bana.

---

## 1b. P0 — jo land ho chuka hai, aur uska measurement

| # | Change | Files | Status |
|---|---|---|---|
| **P0-1** | Tier 0 dedup — `db.find_archive()` ka production caller | `app.py` (`_find_archived_copy`, `_telegram_backup_step`, `_archive_to_db`) | done + tested |
| **P0-2** | `_fingerprint` ab content par, path par nahi | `telegram_uploader.py` | done + tested |
| **P0-3** | `dataset_safe/source_video.mp4` hata; worker `tgup fetch` se maange | `pipeline.py`, `multitasker.py`, `dub_job.json` | done + tested (Kaggle leg untested) |

### P0-1 — Tier 0: `MEASURED`

200 MB blob, real channel `@tgwebcloud1`, real upload:

| | wall clock | bytes on the wire |
|---|---|---|
| **before** (no digest → always upload) | **199.8 s** | 200.0 MB |
| **after** (same bytes, moved path, digest supplied) | **0.00 s** | **0.0 MB** |

Bachta hua: **199.8 s per duplicate upload**, 100% of the run. Archive link
`https://t.me/tgwebcloud1/19`.

Saath hi ek aur cheez measured hui: **app.py pehle poora file do baar hash
karta tha** (line 1314 project-id ke liye, phir `_archive_to_db` mein dobara).
Ab ek baar. 3 GB source par ~3–6 s bachta hai. `DERIVED` (hash cost ka
arithmetic, poora measurement nahi).

### P0-2 — content key: `MEASURED`

Cost, 40 MB file, 3 runs averaged:

| | cost |
|---|---|
| legacy key (`path\|size\|mtime`) | **0.092 ms** |
| **content key (16 MB sample + size + mtime)** | **18.57 ms** |
| poora sha256 | 37.76 ms |
| **content key vs full hash** | **49.2 %** |

Woh 18.57 ms ka price — 34.9 s ke crash-resume ke liye (neeche dekho).

**Resume-across-move, measured.** 40 MB blob, 2 parts (~21 MB each). Poora upload
hua, phir journal ko "part 1 ke baad crash" state me wapas set kiya, phir file ko
`shutil.move` kiya (wahi jo app har resolved link par karta hai):

| | wall clock | parts re-sent | bytes |
|---|---|---|---|
| journal legacy key se reachable | — | **0 parts** | — |
| journal content key se reachable | — | **1 part** | — |
| **before** (unreachable, jaisa old scheme me hota tha) | **65.6 s** | 2 | 40 MB |
| **after** (content key se reachable) | **30.6 s** | 1 | 20 MB |

Bachta hua: **34.9 s (53 %)**, aur 20 MB ke bytes. Upar wali do line seedha proof
hai ki bug maujood tha: legacy key ke paas journal **0 parts** ka tha, content key
ke paas **1 part**.

**Ek honest limitation jo maine khud add ki:** sample sirf head+tail leta hai.
 Beech ka edit (24 MB file ka beech) sample se nahi dikhta — isliye maine key me
**mtime bhi daala**. Move mtime preserve karta hai (same volume = same inode,
跨 volume = `shutil.move` timestamps copy karta hai), edit nahi karta. Do test
isse pin karti hain. Baaqi residual risk (beech ka edit + coarse mtime
filesystem) ka dafa part-level SHA-256 cross-check pakadta hai — corrupt data
silently store nahi hoga.

### P0-3 - dataset se media gaya: `MEASURED` + `DERIVED`

> ### 🔴 RETRACTION (NIMBU ne pakda, 2026-10-08)
>
> **Ye feature pehle DEAD tha.** Mere galat code me likha tha:
> ```python
> worker_fetches = bool(archive_link) and not _env_flag("TDUBBER_WORKER_FETCH", True)
> ```
> `_env_flag` unset par `True` deta hai → `not True` = `False` → **P0-3 default
> me kabhi on nahi hua.** Media aaj bhi dataset me ja raha tha, aur maine ise
> "land" bola.
>
> NIMBU (mimo-v2.6-flash-free) ne `WE_ARE_TEAM.MD` review me ye pakda — **apni hi
> measurement ke against.** Fix ab hai: `_worker_fetches()` (`pipeline.py:546`) +
> wiring test `test_p0_direct.py:385`. Live verify: unset→True, `off`→False,
> youtube→False. **7/7 suites green.**
>
> **Meri reporting galti:** maine do alag features (Tier 0 dedup — sach me chalta
> hai; P0-3 media-hataana — **chalta hi nahi tha**) ek "P0 measured" table me
> mila diya. Neeche ke bytes/s numbers **"agar chalta to"** hain, counterfactual.

| | |
|---|---|
| disk ka raw copy cost (200 MB) | **0.23 s** @ 862 MB/s - `MEASURED` (sirf disk ka, feature nahi) |
| dataset se hata bytes | 200 MB - `MEASURED` bytes, par **feature dead tha** |
| uss par uplink seconds | 200 MB / 1.00 MB/s = **~200 s** - `DERIVED` *(counterfactual)* |
| local 480p encode hata | **abhi tak nahi hua** |

**Production me dataset 480p encode carry karta tha, original nahi.** Toh asli
bacha hua byte count uss encode ka size hai. Upar ka 200 s **upper bound** hai.


### Teen naye bugs jo mere apne changes ne test me pakde

1. **Go journal ka key mismatch.** `_try_go_upload` key khud recompute karta
   tha, digest ke saath. Matlab Go upload ka journal ek naam se likha jata,
   agla run doosre naam se dhoondta — **har "already uploaded" reuse chup-chaap
   dobara upload kar deta**. Fix: `journal_key` param se caller ka key pass hota
   hai. `test_p0_direct.py::GoJournalKeyTests` isko pin karti hai.
2. **Worker ka `subprocess.run` unguarded tha.** Binary check ke baad delete ho
   jaye to `FileNotFoundError` kernel maar deta. Ab `OSError` pe fallback.
3. **Notebook cell 4 P0-3 ko maar deta.** Wo `os.walk` karke pehli video uthata
   tha aur `raise FileNotFoundError` karta tha. P0-3 me dataset me video hai hi
   nahi — to classic single-file path pe kernel **marg raha**. Ye `multitasker`
   bypass karta hai, isliye `discover_jobs` ka fix kaafi nahi tha.
   `patch_notebook_p0.py` ne cell 4 ko ab channel-restore + mounted-video
   fallback ke saath badla hai (idempotent, `nbformat.validate` pass).

### Naye files

| File | Kaam |
|---|---|
| `bench_p0.py` | Measurement harness. `--sections a,b,c,p1`. Har number MEASURED/DERIVED label ke saath |
| `bench_p0_results.json` | A/B ka raw result |
| `test_p0_direct.py` | 23 naye tests — Tier 0, content key, legacy migration, worker fallback contract |
| `patch_notebook_p0.py` | Notebook cell 4 patch (ek baar, idempotent) |

### Test status

```
test_p0_direct.py    23 tests   OK
test_tgup.py         21 tests   OK
test_tg_cloud.py     11 tests   OK
test_flow_order.py    7 tests   OK   (3 stubs ka signature update kiya)
test_db.py           12 tests   OK
test_channel_caption.py 9 tests OK
multitasker_test.py          OK
```

---

## 2. Aaj ka flow (code me verified)

```
user pastes link
  │
  ▼ app.py:1260   inbox_dir = projects/_inbox/<YYYYmmdd_HHMMSS>/
  ▼ app.py:1272   _resolve_link_step() → link_resolver.resolve_to_local_file()
  │                 link_resolver.py:600  outtmpl = <workdir>/<slug>.%(ext)s
  │                 link_resolver.py:865  ydl.extract_info(..., download=True)   ★WRITE #1
  ▼ app.py:1314   source_fingerprint = _fingerprint_file(path)[:12]   (sha256, poora file padha)
  ▼ app.py:1323   shutil.move(inbox → projects/<id>/)                   ★WRITE #2 (rename)
  ▼ app.py:1374   _telegram_backup_step(persisted_video)
  │                 telegram_uploader.upload_file_detailed(PATH)
  │                   go_planner.should_use_go_upload() → True (unconditional, go_planner.py:316)
  │                   tgup upload --file PATH  →  upload.go:124 os.Open + Seek + FromReader ★READ
  │                   (fail → Telethon HashingFileSlice(file_path) → send_file(obj) ★READ)
  │                                                              ★UPLOAD #1 (hamara uplink)
  ▼ pipeline.py:661  shutil.copy(video → projects/<id>/dataset_safe/source_video.mp4) ★WRITE #3
  ▼ pipeline.py      Kaggle dataset upload                            ★UPLOAD #2 (hamara uplink)
  ▼ pipeline.py:484  kaggle kernels output → project_dir              ★WRITE #4
  ▼ (Kaggle) multitasker.py:582  tgup upload --file output.mp4       ★UPLOAD #3 (worker ka uplink)
```

`.tg_uploads/` media nahi rakhta — wo sirf JSON journal hai
(`telegram_uploader.py:75`, disk pe 14 files, sab `.json`, 267–2081 bytes).

**Teen baar hamare uplink se jaata hai wahi source video:** Telegram pe, Kaggle dataset
me, aur (worker side) final dub. Yehi sabse bada kharcha hai.

---

## 3. Ye approach kyu bekar hai — 6 wajah

### R1 — Zero pipelining (sabse bada nuksan)

Download aur upload ek hi uplink share karte hain aur aaj **serial** hain.
Machine ke measured figures (`TGUP.md`): uplink **3.76 MB/s**, RTT **110 ms**,
single-stream MTProto **2.06 MB/s**. 1 GB source, D = 2.06 MB/s (conservative, download
speed = upload speed maankar):

```
aaj      :  1024 MB / 2.06  +  1024 MB / 2.06              = 994 s
overlap  :  2048 MB / 3.76   (ek hi uplink, dono saath)     = 545 s
```

**~45% bachat.** Aur ye bachat tabhi milti hai jab download aur upload **ek saath**
 chale — disk hatane se ye nahi aati.

### R2 — Upload hamesha disk se padhta hai, source kabhi nahi

- `tgup`: `commands.go:166` `--file PATH` mandatory, `commands.go:186` `os.Stat`,
  `upload.go:124-131` `os.Open` + `Seek(offset)`. URL, stdin, fd — koi nahi.
- Telethon: `HashingFileSlice` (`telegram_uploader.py:437`) ek **file handle** hai.
  Ye pipe bhi ho sakta hai, par aisa koi call site nahi hai.

Matlab: hum apna hi data dobara disk se nikal rahe hain. Zero-disk path ka koi
exist karta hi nahi.

### R3 — Disk space amplification

Ek hi source video ki 3 local copies saath-saath hoti hain: inbox → project →
`dataset_safe`. 3 GB movie = **~9 GB peak**. Kaggle pe `/kaggle/working` 20 GB quota
hai aur usme dataset + output dono ginte hain. Media ko rakhna hi galat jagah hai.

### R4 — Crash = zero se restart, aur `move` resume ko **todti** hai

Ye genuine bug-shaped consequence hai:

```python
# telegram_uploader.py:295-303
def _fingerprint(path, size):
    seed = f"{os.path.abspath(path)}|{size}|{os.stat(path).st_mtime_ns}"
    return hashlib.sha256(seed.encode()).hexdigest()[:20]
```

Journal key = **path + size + mtime**. `app.py:1323` file ko move karta hai →
abspath badalta hai → **naya key** → 8-part ka adhoora upload part 1 se restart.
Saath hi yt-dlp ke `.part` fragments inbox me orphan ho jaate hain (sirf final file
move hoti hai).

### R5 — Content-dedup code likha hai par production me **koi caller nahi**

```python
# db.py:115
UNIQUE (fingerprint, channel)

# db.py:567
def find_archive(fingerprint, channel):   # "cache hits" ke liye
```

`find_archive` sirf `test_db.py:141` me call hota hai. `app.py:635` ka rule
("source already Telegram me hai → dobara upload mat kar") sirf `t.me` source pe
lagta hai, normal YouTube link pe nahi. Toh har naye job me wahi 1 GB dobara jaata hai.

### R6 — Aur ek honest sawaal jo log nahi poochte

**Disk write hata kar akele ~1% deta hai, 45% nahi.**

NVMe ~1–3 GB/s: 3 GB write + 3 GB read ≈ 3–6 s. 545 s ke operation me wo ~1% hai.
Matlab "disk nahi chahiye" akela ek argument **kamzor** hai. Asli argument ye hai:
disk-first design **serial** hai; pipeline banana hi asli jeet hai, disk hataana
uska byproduct hai. Jo doc ye claim kare ki "disk hataane se 2x fast" — wo galat hai.

---

## 4. Kitne approaches hain: 5 viable + 1 rejected

### A1 — Telegram server-side fetch: `inputMediaDocumentExternal`

Telegram **khud** file download karta hai. Hamara uplink ~0 bytes.

- **Available hai, verified.** Telethon 1.45.0 `client/uploads.py:817-821` par
  `file="https://..."` ko `InputMediaDocumentExternal` map karta hai.
- **Live schema me confirmed:** `inputMediaDocumentExternal#779600f9` —
  *"Document that will be downloaded by the telegram servers"* (layer 225),
  aur `messages.sendMedia` me `EXTERNAL_URL_INVALID` error exist karta hai.
- **Ceiling: photo 5 MB, baaki 20 MB** (HTTP-URL media ka documented limit).
  Cookies/headers/custom UA nahi jaate. Telegram ka fetcher yt-dlp nahi chalata —
  YouTube watch URL ek HTML page hai, file nahi → fail.
- **Verdict:** thumbnail / chhoti clips / direct-CDN files ke liye perfect. Movies ke liye bekaar.

### A2 — Bot API `sendDocument` / `sendVideo` with URL

- Cloud Bot API: wahi 20 MB URL rule, multipart pe 50 MB.
- Local Bot API server (self-host, VPS chahiye): 2000 MB upload + `file://` URI scheme.
- Hum aaj **user session** use karte hain (Telethon), bot nahi. Bot ko har channel
  ka admin banana padega — ek naya permission dependency.
- **Verdict:** main path ke liye nahi. Thumbnail fallback rakho.

### A3 — Local stream: `yt-dlp | ffmpeg` → Telethon file object (zero disk)

```python
proc = subprocess.Popen([...,"-movflags","+faststart","-f","mp4","pipe:1"], stdout=PIPE)
client.send_file(channel, file=proc.stdout, file_size=known_size)
```

- Pattern prove ho chuka hai (`HashingFileSlice` wahi contract hai) — sirf source badalna hai.
- **Problem 1:** MP4 ka `moov` atom end me hota hai. `+faststart` ke bina Telegram
  ko duration/poster nahi milega. `-movflags +faststart` = poora read-through
  (re-encode nahi, CPU sasta), par **size pehle se jaanna padega**.
- **Problem 2:** ek hi MTProto connection = **2.06 MB/s ceiling**. Fast banane me
  easy, 2x slow.
- **Verdict:** fallback tier. Yaad rakhna — `-` (stdout) ka idea sirf PCM me use
  hua hai (`mazinger/.../validate.py:34`), video ke liye pehli baar.

### A4 — `tgup --url`: ranged parallel pull + N MTProto connections ← **MERI CHOICE**

Server side pe **ek hi line** badalni hai:

```go
// aaj: upload.go:124-131
file, err := os.Open(u.path)
file.Seek(p.Offset, io.SeekStart)
up.FromReader(ctx, name, io.LimitReader(file, p.Size))

// baad me:
src, err := openSource(spec)          // osFile | httpRange | tailFollow
io.NewSectionReader(src, p.Offset, p.Size)
up.FromReader(ctx, name, section)
```

`io.ReaderAt` interface ke peeche 3 implementations:
`osFile` (aaj wala), `httpRange` (naya), `tailFollow` (A7).

- Har goroutine apna byte window origin se **ranged GET** karke apne Telegram DC me
  push karta hai → N downloads + N uploads, disk zero.
- Multi-window mechanism pehle se exist karta hai aur default hai
  (`go_planner.py:316` — `should_use_go_upload()` unconditional `True`).
  Yaani main **naya path nahi, live path extend** kar raha hoon.
- Integrity waisi rehti hai: SHA-256 cross-check (`upload.go:238`,
  `telegram_uploader.py:1137-1147`) bytes stream hote hue hi hota hai.
- Range probe ka code already hai — `chop_drop.py:140-161` (`HEAD` + `bytes=0-0`).

**Verdict:** zero disk **aur** N windows dono. Isi machine pe sabse fast.

### A5 — Relay / origin: Telegram ko hamara apna public URL

`edge/` (Go, HF Space) already `Range` + `ETag` deta hai `/artifact/<name>` par
(`edge/server.go:306-320`). Agar fetch edge pe land kare, to Telegram wahin se
kheench sakta hai.

- **Jab jeetna hai:** wahi file **kai channels** pe jaa rahi ho → ek baar fetch,
  N baar upload. Ya jab origin pe Range nahi hai. Ya thumbnail ke liye A1 fallback.
- **Jab nahi jeetna:** har byte phir bhi hamare uplink se ek baar jaata hai.
  Ye A4 ka replacement nahi, **amplifier** hai.
- `edge` deliberately GET-only hai ("no POST/PUT/DELETE", `edge/README.md:96`) —
  security model hi aisa hai. Media dene ke liye use mat karo.

### A6 — Cloudflare Worker + `connect()` se MTProto — **REJECTED**

MTProto ka obfuscated2 raw TCP chahta hai; Workers me CPU/bandwidth caps aur GB
push karne par billing + abuse risk. `cloudflare_postman.js` aaj bhi sirf
**webhook → Kaggle kernel trigger** hai, koi media resolver nahi (poore repo me
zero imports). Faisla: **nahi** — sirf agar kabhi 10 MB se chhoti koi cheez ho.

### A7 — Tail-follow the growing file (A4 ka page-URL wala brother)

YouTube/Instagram ke liye yt-dlp chalana **zaroori** hai (page URL, extractor chahiye),
aur yt-dlp merged video+audio ko ek stream me nahi de sakta. Ye approach:

1. yt-dlp `<proj>/source.mp4.part` me likhta rahe.
2. Saath hi `tgup --follow` chalta hai: `tailFollow` ReaderAt file ko grow hote dekh
   kar **sirf poora poora** range upload karta hai.
3. Part count final size se aata hai — yt-dlp ka `filesize` / `filesize_approx`
   info dict se mil jaata hai, ya last part ke liye mtime-detect se band karte hain.
4. Poora hote hi `.part` → final rename, phir **delete**.

**Isse mile:** full overlap (R1 ka poora 45%), zero extra copy (R3/R4 gone),
sabse bada problem (unknown size) bhi solve. Yehi teri real workload ke liye
sabse important approach hai — kyunki tere links mostly YouTube hain.

---

## 5. Scorecard

| | A1 external | A2 Bot API | A3 local pipe | **A4 tgup --url** | A5 relay | A7 tail-follow |
|---|---|---|---|---|---|---|
| Hamara uplink | **0 B** | 0 B | poora | poora (N par) | 1x | poora (N par) |
| Max size | 20 MB | 20 MB (2000 local) | 2 GB | **2 GB / part** | unlimited | 2 GB / part |
| MTProto windows | — | — | 1 | **N (3–8)** | N | **N (3–8)** |
| Disk copies | 0 | 0 | 0 | **0** | 1 | **0** |
| Range chahiye? | nahi | nahi | nahi | **haan** | nahi | nahi |
| Size pehle se pata? | haan | haan | **haan** | **haan** | nahi | **nahi** |
| YouTube/IG link | nahi | nahi | haan | nahi | nahi | **haan** |
| Code change | 5 lines | naya bot admin | ~40 lines | **~250 lines Go** | ~80 lines | **~120 lines Go** |
| Verdict | thumbnail only | reject | fallback | **DEFAULT** | multi-dest | **page-URL default** |

---

## 6. Meri choice: ek hi entry point, 6 tiers

Ek function, `upload_from_link(url, channel, ...)`, jo pehle classify kare aur phir
sahi tier pe jaye. Har tier ka apna trigger clear hai, koi guess nahi.

| Tier | Kab | Kya | Uplink |
|---|---|---|---|
| **0** | `db.find_archive(sha256, channel)` hit | purana link return | **0** |
| **1** | size ≤ 20 MB **aur** direct file URL | A1 `InputMediaDocumentExternal` | **0** |
| **2** | direct file URL + Range + Content-Length | **A4 `tgup --url`** | N-window |
| **3** | page URL (YouTube/IG…) | **A7 tail-follow** | N-window |
| **4** | Range nahi, size pata nahi | A3 local pipe | 1-window |
| **5** | ffmpeg ko seekable file chahiye (merge + burn subs) | aaj wala disk path | N-window |

### Ye order kyun?

- **Tier 0 pehle** — kyunki `db.py:567` likha hua hai par caller nahi. Ye 1 line ka
  kaam hai aur 100% upload bytes bacha sakta hai. Sabse pehle ye lagao.
- **Tier 1 sabse upar** — kyunki 0 bytes. Par 20 MB ceiling ki wajah se movies ke
  liye kabhi nahi chalega. Isliye default nahi, **opportunistic** hai.
- **Tier 2 default isliye nahi ki tez hai** — isliye ki ye **ek** hi approach hai
  jo zero-disk **aur** multi-window **dono** deta hai. Baaki sab me se ek kamzor
  link toot-ta hai.
- **Tier 3 alag kyun** — kyunki yt-dlp ka output ek stream nahi hai. Ye A4 ka
  generalization nahi, ye uska alag case hai.
- **Tier 5 aaj bhi zaroori** — burned-in subtitles ke liye ffmpeg ko output me
  `seek` chahiye. Sipaai pipe nahi kar sakta. Ye accept karo, isko hataane ki
  koshish mat karo.

### Aur ek structural fix (isse zyada bacha)

`pipeline.py:661` ka `dataset_safe/source_video.mp4` **band karo**. Worker ko media
Kaggle dataset se nahi, **channel se** leni chahiye:

```
aaj : local → Telegram (u1)  +  local → Kaggle dataset (u2)  → worker ne dataset se liya
baad: local → Telegram (u1)                                 → worker ne `tgup fetch` se liya
```

`tgup fetch --link https://t.me/name/123 --concurrency 4` **pehle se maujood** hai
(`TGUP.md:158-159`, `fetch.go`) aur har part ka digest verify karke join karta hai.
Matlab integrity end-to-end bani rehti hai, aur humara uplink se **ek poora round-trip
hat jaata hai** — jo local disk changes se bhi zyada bacha hai.

---

## 7. Technology decision — kis technology, kyu

| Kaam | Technology | Kyu |
|---|---|---|
| Multi-window MTProto upload | **Go — `tgup` extend** | `gotd` pool + goroutines pehle se hain; Python `asyncio` se ek client = ek TCP window = 55% link utilisation (`END_TO_END_ARCHITECTURE_REPORT.md` ka measured conclusion) |
| HTTP Range + tail-follow source | **Go, `io.ReaderAt`** | `io.SectionReader` = byte-exact windowing, allocation-free; Python me `os.pread` + thread pool se same kaam 3x adhara code |
| ≤20 MB external case | **Python — Telethon** | `gotd v0.162.0` me `InputMediaDocumentExternal` **hi nahi hai** (module cache me `tl_input_file_gen.go` se confirmed: sirf `InputFile`, `InputFileBig`, `InputFileStoryDocument`). Go me karne ke liye TL schema hand-roll karna padega — 250 lines fragile code vs 5 lines tested library |
| Worker side restore | **Go — `tgup fetch`** | Already multi-part, digest-verified |
| Dedup | **SQLite (`db.py`)** | `UNIQUE(fingerprint, channel)` already hai; sirf caller chahiye |

### Decision log — kya reject kiya aur kyun

| Rejected | Kyun |
|---|---|
| Bot API ko main path banana | Naya admin dependency har channel pe; 20 MB ceiling; user session ka fayda kho dete hain |
| Cloudflare Worker se MTProto | Obfuscated2 raw TCP; Workers CPU/BW caps; GB push par billing + abuse |
| `edge/` ko media relay banana | Uski security model jaan-boojh kar GET-only hai; aur wo bytes ko ek baar phir bhi hamare uplink se laata — amplifier hai, replacement nahi |
| Naya language (Rust streaming source) | `stitcher`/`subtitle_forge` Rust me **DAANI** kaam karte hain (timeline → WAV bytes ka stream). Yahan kaam MTProto + HTTP range hai — Go me pehle se library hai |
| Multipart album upload | Multi-part resume + manifest + cross-check ka poora system single-message pe bana hai; album badalne se resume/verify dono tootenge |

---

## 8. Implementation plan

### Go — `tgup` (naya `source.go`, ~250 lines)

```go
// Source: byte-exact windowed access, no disk required.
type Source interface {
    io.ReaderAt
    Size() int64
    Close() error
}

func openSource(spec Spec) (Source, error)   // osFile | httpRange | tailFollow
func probeRange(url string) (size int64, ok bool)  // HEAD + Range: bytes=0-0
```

- `commands.go`: naye flags — `--url`, `--stdin` (media, credentials ke alawa),
  `--follow` (tail mode), `--expected-size`. `--file PATH` **bilkul same chalta rahe**.
- `upload.go:124-131`: `os.Open`+`Seek` ki jagah `openSource` + `io.NewSectionReader`.
- Part count `--expected-size` se, warna `--follow` ke andar grow-detect se.
- `plan` command bhi `--url` accept kare, warna digest manifest bana hi nahi payega.

### Python

| File | Kaam |
|---|---|
| `link_resolver.py` | naya `resolve_to_stream()` → `(kind, url, size, headers)` — **bina download kiye**. `resolve_to_local_file()` bilkul unchanged (Tier 5/backup ke liye zaroori) |
| `go_planner.py` | `upload_url_via_go()` + `should_use_go_url()`; `should_use_go_upload()` ki tarah guard-style |
| `telegram_uploader.py` | **P0-1 done** — `_find_archived_copy()` + `_telegram_backup_step(..., content_sha256=)`. Baaki 6-tier router (A1–A4) P2 ke saath |
| `telegram_uploader.py:295` | **P0-2 done** — `_fingerprint(path, size, digest=None)`, content + mtime. `_legacy_fingerprint()` migration ke liye bacha hai |
| `pipeline.py:661` | **P0-3 done** — media copy hata, `_channel_archive_link()` gate, `dub_job.json` me `fetch_from_telegram`/`source_sha256`/`compress_480p_on_worker` |
| `multitasker.py` | **P0-3 done** — `restore_source_from_channel()`, `discover_jobs` fetch mode, digest verify, worker-side 480p |

### Kill switch

Already landed (P0):

```bash
TDUBBER_WORKER_FETCH=off   # media wapas dataset me jayega — full revert of P0-3
```

P2 ke saath aayega:

```bash
TDUBBER_DIRECT=off        # aaj wala disk-first flow — full revert
TDUBBER_DIRECT_TIER=2     # force ek hi tier (debug/compare)
TGUP_MAX_CONNECTIONS=4    # FLOOD_WAIT se bachao; bench se knee chuno
```

Koi bhi tier fail → **next tier**, aur last me aaj wala path. Har failure ek reason
string ke saath journal me (`_try_go_upload` ka return-None pattern, `telegram_uploader.py:677`).

### P0 me jo nahi badla (jaan-boojh kar)

- **`resolve_to_local_file()` chhua hi nahi.** Tier 5 (seekable file chahiye) aur
  har non-happy-path is par depend karta hai. P0 disk hata nahi raha — wo A4
  (P2) ka kaam hai.
- **`should_use_go_upload()` unconditional `True` rakha.** P0 ka scope nahi tha,
  aur ye baat Section 9b me alag se address ki gayi hai.
- **Kaggle dataset abhi bhi banta hai** — usme code + `dub_job.json` jaata hai,
  sirf media nahi. Dataset versioning aur mazinger bundling waise hi chalti hai.

---

## 9. Verification — kya prove hai, kya nahi

### Abhi prove hai (repo me verified)

- gotd v0.162.0 me `InputMediaDocumentExternal`/`InputFileURL` nahi → Go me A1 possible nahi.
- `inputMediaDocumentExternal#779600f9` live schema me hai, layer 225, "downloaded by the telegram servers".
- Telethon 1.45.0 URL → external map karta hai (`uploads.py:817-821`).
- `tgup` sirf `--file PATH` leta hai (`commands.go:166,182-194`); URL/stdin/fd ka koi path nahi.
- Repo me **koi bhi jagah** download→upload stream nahi hota; `requests stream=True` ka zero use.
- ~~`db.find_archive()` ka koi production caller nahi~~ → **P0-1 me add ho gaya.**
- ~~`_fingerprint` path-based hai~~ → **P0-2 me content-based ho gaya.**

### `MEASURED` (2026-10-08, is machine par)

| Cheez | Number |
|---|---|
| 200 MB upload, pehli baar | **199.8 s → 1.00 MB/s** |
| 200 MB upload, wahi bytes dobara (Tier 0) | **0.00 s, 0 bytes** |
| 40 MB crash+move resume, pehle | **65.6 s (40 MB)** |
| 40 MB crash+move resume, baad me | **30.6 s (20 MB)** |
| content key cost (40 MB file) | **18.57 ms** (poore hash ka 49.2 %) |
| 200 MB `shutil.copy` (jo hata diya) | **0.23 s @ 862 MB/s** |
| **tgup session** | **UNAUTHORIZED** |

### Abhi bhi `PREDICTED` (measurement nahi)

- **N connections se speed badhegi.** Is document ka poora "2.7x" claim isi par
  tika hai, aur ye **abhi tak measure nahi hua** — kyunki tgup chal hi nahi raha.
- 45% / 2.7x / 3.7x numbers — arithmetic hai inputs par, wo inputs abhi galat
  (ya outdated) hain.

---

## 9b. 🔴 P1 BLOCKED — Go fast-path zinda nahi (measured)

Ye section P1 ka result hai, aur result ye hai ki **P1 chal hi nahi paya.**

`tgup.exe bench` ka exact output:

```
bench failed: callback: not authorized yet and no --phone given;
              run once with --phone to log in
```

Aur ek chhoti upload ka exact progress log:

```
> Go parallel upload x3 (Attempting Go upload unconditionally for maximum speed)
> Go upload unavailable (tgup upload failed: callback: no authorized session at
  C:\Users\pocot\Music\T_Dubber\tgup.session and stdin is not a terminal;
  refusing to request a login code ...); Telethon fallback
> Uploading go_probe.bin (5.00 MB) as 1 part to @tgwebcloud1
```

### Iska matlab

| Fact | Evidence |
|---|---|
| `tgup.session` file **mojood** hai (4197 bytes, aaj 8:07 PM touch hua) | `ls *.session` |
| Par wo **unauthorized** hai | `tgup bench` ka error |
| Isliye har upload Telethon par girta hai | progress log upar |
| `go_planner.py check` ye **false all-clear** deta hai | `"session": true, "needs_login": false` |
| Kyun? | `tgup_bridge.py:241` → `session_ready()` sirf `session_path().is_file()` check karta hai. **Authorization check hi nahi hai.** |

**Consequence:** multi-window upload ka mechanism (`END_TO_END_ARCHITECTURE_REPORT.md`
ka 3.76 MB/s vs 2.06 MB/s wala论证) **aaj in effect nahi hai**. Har upload
single-stream Telethon hai. `TGUP.md` ka "2.06 MB/s single-stream" figure
sirf tab applicable tha jab Go chal raha tha.

### P1 ke liye ek hi command chahiye — aur wo aapke console me

Main ye **nahi** chala sakta, aur jaan-boojh kar nahi chala: tgup login flow
shuru hote hi Telegram OTP bhej deta hai, aur headless machine par us code ko
type karne ka koi tareeka nahi. Us run ka matlab hoga ek **bekaar OTP** aur phir
wahi failure. Isi liye `loginDecision()` ye refuse karta hai — ye sahi design hai.

Aapke console me, ek baar:

```
cd C:\Users\pocot\Music\T_Dubber
.\tgup.exe upload --file dummy.mp4 --channel @tgwebcloud1 `
  --api-id 35578684 --api-hash <hash> --phone +919286175802
```

(API hash `config.json` me DPAPI-encrypted hai; `app.load_tg_config()[1]` se nikal
sakta hai, ya aap GUI ke ☁️ Telegram Drive tab se dekh sakte hain.)

Uske baad:

```
python bench_p0.py --sections p1
```

**P2 (source.go) tab shuru hoga jab ye number positive aaye.** Abhi nahi.

### Kaise prove karein

```bash
# 1. Speed ka knee — 1,2,3,4 connections (already exists)
python go_planner.py bench --channel @tgwebcloud1 --api-id N --api-hash H

# 2. ReaderAt correctness — Go me fake Range server (httptest), bytes + digest assert
go test ./... -run TestSource -v          # naya: TestHTTPReaderAt, TestTailFollow, TestNoRangeFallsBack

# 3. Tier router — byte-identical file, path vs --url, same manifest
python test_tgup.py                       # extend: part digests equal
python test_flow_order.py                 # extend: assert ZERO disk writes on tier 2/3

# 4. End-to-end — real channel, real link
python e2e_upload.py
```

Pass criteria: tier 2/3 ka manifest digest **byte-identical** ho aaj wale path se.
Wahi repo ka existing standard hai (`test_tgup.py` part layout + digest equality pin karta hai).

---

## 9c. 🔴 A4 ka official counter-argument — mere hi plan ke against

`core.telegram.org/api/optimisation` se verbatim:

> *"We recommend that separate connections and sessions be created for these
> tasks. Remember that the extra sessions must be deleted when no longer needed.
> It makes sense to **download** files over several connections (optimally to have
> a pool). **When uploading data to a server one connection is enough to achieve
> the best results.**"*

Aur wo optimal pattern batata hai — ek hi connection par sliding window:
*"two or more queries continuously being executed through one connection"*.

### Iska seedha asar

| Cheez | Pehle likha tha | Ab |
|---|---|---|
| `TGUP.md` ka poora thesis (pool of clients) | "the whole mechanism" | **official guidance ke against** |
| Meri A4 (N connections = fastest) | "sabse fast" | **galat bhi ho sakta hai** |
| 2.7x claim | "2.7x faster" | `PREDICTED`, official doc ke against |

### Dhokha nahi, nuance

Community alag kehta hai:
- `teleproto.dev`: *"more workers (more parallel parts) beats any amount of
  per-part tuning"* — "Telegram throttles per connection".
- `tdl` issue #52: official client par 100 Mbps, `tdl` par **32 threads** chahiye.
- Telethon ke maintainer ka gist: parallel file transfer, workers badhao, zyada
  workers → `floodwaits`.

**Toh official docs aur field practice me disagreement hai.** Isko settle karne ka
ek hi tareeqa hai — `tgup bench --concurrency 1,2,3,4`.

**Decision rule ab ye hai:**
> Agar N connections se fark nahi padta — to wo **bug nahi, documented behaviour
> hai**, aur A2 (server-side fetch, 0 bytes uplink) hi asli jeet hai.

Isiliye P2 ko **bench ke bina start nahi karna**, aur bench ka result
official doc ke against interpret karna — apni pasand ke against nahi.

---

## 9d. Ek hypothesis jo maine maara — khud ko, turant

Socha tha: *"shayad Telethon pure-Python AES me hai (cryptg missing), isliye
upload CPU-bound hai, isliye TCP window tuning bekaar hai. Bada finding."*

Check kiya. **Galat tha:**

```
cryptg: installed
AES-256-IGE: 1.21 ms per 512 KB part -> 414.8 MB/s
1 GB encrypt karne ka CPU: 2.5 s
```

Encryption bottleneck **nahi** hai. 1.00 MB/s genuinely network/path-bound hai.

Ye likha jaana chahiye kyunki ye wahi galti hai jo repo ke `AGENTS_MAP.md` me bhi
documented hai — apni taraf se *"60x faster asm likha, phir delete kiya"*. Wahi
discipline, chhota version. **Bina stopwatch ke "bada finding" bolna galat hai,
chahe hypothesis kitni bhi achi lage.**

---


## 10. Risks aur ghotte (jo mujhe pata hain)

1. **Signed URLs expire.** `googlevideo.com` signed link ~6 ghante me expire hota hai.
   Isliye `--url` pe **resume unsafe** hai — crash ke baad wahi signed URL dobara
   chalega to `403`. Isliye Tier 3 (tail-follow) resume ke liye behtar hai: wo hamare
   apne disk pe hai, signed URL ki zaroorat hi nahi. Ya har resume pe URL dobara resolve karo.
2. **FLOOD_WAIT.** N connections ek saath = N× flood risk. `--concurrency` default 3,
   cap 8. `bench` se **sabse kam** wahi value jo best rate de. Extra sockets baad me
   `FLOOD_WAIT` dilwa kar nuksaan karte hain (`TGUP.md` "Tuning").
3. **Range nahi mila to fallback.** `chop_drop.py:143` ka comment sahi hai — bina Range
   ke har chunk byte 0 se download hota hai = O(n²) disaster. Isliye `probeRange()`
   fail → seedha Tier 4, "try karte rahein" nahi.
4. **20 MB wala trap.** Tier 1 pe koi bhi `EXTERNAL_URL_INVALID` aaye to **Turant**
   Tier 2 pe jaana hai, retry same tier pe nahi. Ye error Telegram ke fetcher se aata hai.
5. **SSRF guard reuse karo.** `link_resolver.py:382` `assert_fetchable_url()` —
   per-address `is_global` check. Naye `--url` path ko iske **bina** kabhi mat chalao.
6. **MP4 `moov`.** Streamed upload me `-movflags +faststart` zaroori, warna Telegram
   preview/duration nahi dikhata. Ye re-encode nahi hai — CPU sasta, par read-through hai.
7. **2 GB ceiling.** `CHUNK_SIZE = 1900 MiB` (`telegram_uploader.py:79`) —
   non-Premium Telegram ka limit. Isko badhao mat jab tak Premium na ho.

---

## 11. Numbers — ab kya pata hai, kya nahi

P0 ke baad **measured** inputs (is machine par, 2026-10-08):

| Input | Value | Kind |
|---|---|---|
| Telethon upload rate (Go band hai) | **1.00 MB/s** | `MEASURED` |
| `tgup` multi-connection rate | **unknown** | `PREDICTED` (bench blocked) |
| local disk copy | 862 MB/s | `MEASURED` |
| content-key cost | ~9 ms / 40 MB | `MEASURED` |

### Jo abhi tak pata hai (measured)

| Change | Pehle | Baad | Kind |
|---|---|---|---|
| Duplicate source upload (200 MB) | 199.8 s | **0.00 s** | `MEASURED` |
| Crash-resume after a move (40 MB, 2 parts) | 65.6 s | **30.6 s** | `MEASURED` |
| Dataset se media (200 MB) | feature **dead** tha | ab theek, par **abhi koi real run nahi** | `RETRACTED` |
| Poora file dobara hash (app.py) | 2 passes | **1 pass** | `DERIVED` |

### Jo abhi bhi anjaan hai

| Claim | Kyu pending |
|---|---|
| 45% download/upload overlap | Input rate galat hai — measured 1.00 MB/s, doc me 2.06 MB/s maana gaya tha. Naya number P1 ke baad |
| N connections se speed badhegi | **Bench hi nahi chala** — session unauthorized |
| 2.7x / 3.7x total | Upar ke dono par tika hai |

**Seedha baat:** P0 ne **duplicate uploads** khatam kar diye aur **ek poora
uplink round-trip** hata diya. Ye real hai aur measured hai. Lekin **throughput**
abhi bhi wahi hai — 1.00 MB/s — kyunki Go fast-path mar chuka hai aur maine
jaan-boojh kar use nahi chhoda (Section 9b). **Speed ka asli kaam P1 ke login
ke baad shuru hota hai, Stage 1 (source.go) uske baad.**

---

## 12. Ek line mein

> Disk-first design bekaar **isliye nahi** ki disk slow hai (wo ~0.2 s per 200 MB
> hai, ~1%) — bekaar isliye ki ye **serial** hai aur source video ko hamare uplink
> se teen baar bhejta hai. P0 ne do teenon hata diye: duplicate upload ab **0
> bytes** hai (measured 199.8 s → 0.00 s), aur dataset se media gaya to ek poora
> round-trip bacha. **Baaki speed ka kaam P1 ka hai, aur P1 ek interactive login
> par atak gaya hai jo maine nahi chala — kyunki bekaar OTP kharach karega.**
> Jaise hi session authorized hoga: `python bench_p0.py --sections p1`.

---

*Likha: 2026-10-08 · Stack: Python + Go (tgup) + Rust + C++, koi naya language nahi.*
*Har number MEASURED / DERIVED / PREDICTED — koi guess nahi.*

---

# 9.5. `--url` LANDED — kya bana, kya MEASURED hua (2026-10-09, NEEL)

*(Ye section upar ke Section 12 ke baad rakha hai, taaki mojibake wale purane
lines chhede na pade. Padne wala pehle ye dekhe, phir upar jaaye.)*

Ye woh Stage 1 hai jiska is doc ne intezaar kiya tha. P1 bench cancel hone ke baad
(Tarun, 17:40) ye **OTP ka wait hatane** wala kaam tha — aur wo **bina OTP ke**
prove hota hai, kyunki proof byte equality se aata hai.

**Bane hue file:** `source.go` (naya), `source_test.go` (naya),
`tools/tiny_range_server.py` (naya).
**Chhote edit:** `commands.go` (`--url`, XOR guard, `--chunk-size 1MB` units),
`upload.go` (uploader ab `source` leta hai), `main.go` (`Plan.Source`),
`main_entry.go` (help text).
**Python:** `tgup_bridge.upload(url=...)`, `go_planner.upload_url_via_go()`,
`test_tgup_url.py` (10 tests).

**MEASURED (sab apna run, 2026-10-09):**

| Check | Result |
|---|---|
| `go test ./...` | root **ok 2.525s** (13 naye tests), edge-publish **ok 1.615s**, edge **ok 3.783s** |
| `go vet .` / `go build ./...` | clean |
| URL plan vs FILE plan (3,145,728 B random, 1 MB chunks) | 4 parts, **digests equal: True**, offsets equal: True, filename `movie.mp4` |
| `tgup upload --url ... --dry-run` (no credentials, no session) | **exit=0**, `source_sha256=4008299ab23b06b4...` — file ke digest ke barabar |
| Range-supporting origin par bytes served | **1 GET, 3145728 bytes** (payload bhi 3145728) |
| `%TEMP` file count, run ke aage-paas | **1188 → 1188** (delta 0) |
| `python -m pytest` (root suite) | **120 passed in 74.83s**, prod `projects` delta **0** |
| `python -m pytest test_tgup_url.py` | **10 passed** |

**Range na mile toh kya hota hai:** `errNoRange` — tgup **chup-chaap download
nahi** karta. Aur `--file`/`--url` dono ya koi nahi = exit 2, koi guess nahi.

**Resume ka naya guard:** `Plan.Source` (absolute path, ya **redacted** URL —
query hatayi hui, taaki signed token na toote). Plan dusre source ka ho to
resume **fresh** shuru hota hai; warna do files ka jura-ban archive ban jaata,
jo dekhne me theek lagta hai par chalta nahi.

**Hashing ek pass me:** pehle har part ka digest **aur** phir poora source
dobara hash hota tha. File par mehnga nahi, **URL par ek poori extra download**.
Ab `hashSource()` ek hi stream se dono banata hai.

**Abhi bhi nahi bana (jaan-boojh kar chhoda):**

1. **App-level switch** — `TDUBBER_DIRECT` / `TDUBBER_DIRECT_TIER` /
   `TGUP_MAX_CONNECTIONS` (is doc ke lines 522-526) **code me abhi bhi 0 hain**
   (`MEASURED`). `--url` abhi sirf `tgup` aur `go_planner` tak pahunchta hai.
   **`app.py` ka link flow abhi bhi `resolve_to_local_file()` se disk par
   laata hai** (`app.py:581`, `app.py:1339`) — aur wahan `--url` lagana
   **jaan bujh kar nahi kiya**, kyunke wo **galat** hoga:

   > **Wajah (code padh kar):** us flow ka agla step **dub/translation** hai,
   > jise **local file chahiye**. `resolve_to_local_file()` sirf "download"
   > nahi karta — wo us **project directory** ko banata hai jismein pipeline
   > kaam karta hai (quality gate, `pipeline.py:751` ka copy, Kaggle dataset).
   > `--url` wo file **kabhi** nahi banata. Toh Drive-tab ke link flow me
   > `--url` lagana matlab "download mat kar" = "dub mat kar" — jo user ke
   > mann ke bilkul ulta hai.
   >
   > **`--url` sirf us flow me sahi hai jahan video ko Telegram par archive
   > karna hai aur kuch nahi** — aisa mode **aaj exist nahi karta**
   > (`MEASURED`: `app.py` me koi `archive_only` / `only_upload` flag nahi).
   >
   > **Ye product decision hai, code nahi:** @Tarun — *"kya ek 'sirf archive
   > karo, dub mat karo' mode chahiye?"* Agar haan, to `--url` us mode ka
   > raasta ban jayega aur 9 GB ka link **bina disk ke** channel me chala
   > jaayega. Agar nahi, to `--url` CLI/`go_planner` par rehta hai — jo aaj
   > ki takat me theek hai: har jagah callable, par kisi par force nahi.
2. **Page URLs (YouTube)** — `--url` sirf **direct media** ke liye; A7
   tail-follow alag hai.
3. **Real Telegram send** — `--url` ka end-to-end upload **OTP/session** chahta
   hai, isliye wo **PREDICTED** hai. Dry-run + byte equality **MEASURED** hai.
   Dono alag cheezein hain, aur is doc me dono alag likhe gaye hain.
4. **Signed-URL expiry** — resume ke dauraan token rotate ho jaye to range
   requests fail honge; abhi `--plan-in` block nahi hota, sirf source guard hai.

**P1 (throughput) ka status:** owner ne **17:40 par cancel** kar diya. Is doc
me uske numbers **PREDICTED** hi rahenge — koi "2.7x" claim MEASURED nahi hua,
aur ab koi us par depend nahi karega.

---

# 9.6. Zero-disk archive (link -> Telegram, 0 bytes on this PC)

*(2026-10-09. Ye section 9.5 ke baad. Upar ke mojibake lines chhede nahi gaye.)*

9.5 `--url` ko sirf `tgup` aur `go_planner` tak pahuncha ruka tha. Uska matlab
tha: **aap page URL paste karo, wo disk par aayega, phir Telegram par jayega.**
Matlab 9 GB ka video = 9 GB ki jagah iss laptop par, us bhi us waqt chahiye
jab disk khaali ho. Owner ka maang: *"0% PC use -- jahan storage ki baat ho,
Telegram use karo."* Wo **aadha** feature tha, aur aadha reh gaya.

**Ye raha wo doosra aadha.**

## Kya bana

**Naya:** `direct_archive.py`, `test_direct_archive.py`.
**Edits:** `link_resolver.py` (`resolve_to_stream` + helpers),
`go_planner.py` (`--result-out` fix), `tgup_bridge.py` (dry run par session
gate hataya), `pytest.ini` (naya file suite me).

### 1. `link_resolver.resolve_to_stream(url, timeout=15.0)`

Ek link ko aise kuch me badalta hai jo tgup part-by-part stream kar sake -
**is PC par ek byte likhe bina**.

`StreamTarget(kind, url, size, filename, page_url, direct, reason, title, extractor)`

* **Direct media URL** - `assert_fetchable_url()` (SSRF guard, line ~382) aur
  `_looks_like_direct_media()` pehle chalte hain, phir size probe hota hai:
  `HEAD` pehle, aur agar `HEAD` size nahi de (403/405/501 - signed CDN URLs par
  common) to ek **`Range: bytes=0-0` GET**, jiska `Content-Range` total deta hai.
  Range na mile to **`direct=False` + reason** - download kabhi nahi.
* **Page URL (YouTube etc.)** - `yt-dlp` **simulate-only** mode: `skip_download`
  + `simulate` + `extract_info(download=False)`. Direct media URL + size + title
  nikalta hai, phir khatam. **Ek byte nahi.**
* Telegram link, ya yt-dlp na ho, ya link unsupported - `direct=False` / typed
  error, dono me **bhi koi download nahi**.
* Kill switch: **`TDUBBER_DIRECT_STREAM`** (default on).
* `app` / `db` import **nahi** hote.

Ek baat jo code padh kar pata chali: pehle maine `Content-Length` dekh kar
`ranges=True` maan liya tha. Galat hai - bohat se origins header bhejte hain aur
`Range` phir bhi ignore kar dete hain. Ab `Accept-Ranges: bytes` ke saath ek
**asli 1-byte range** maanga jaata hai, aur **sirf 206** par range maana jaata
hai. Warna tgup ko wahi URL di jaati jise wo pakka fail karega, aur wo failure
network blink jaisi lagti hai.

### 2. `direct_archive.archive_link(url, channel, api_id, api_hash, ...)`

Resolve -> tgup -> SQLite mirror. **`file_path` me URL hota hai, path nahi** -
kyunki local file hai hi nahi, aur column ka kaam ye batana hai ki protected
bytes kahan se aaye.

**Fingerprint content-derived hai** (`source_sha256[:32]`, wahi truncation jo
`app._archive_fingerprint` aur `db` use karte hain). Toh agli run par wahi bytes
= **Tier-0 hit**, dobara upload nahi.

**`dry_run=True`**: plan + digests, kuch bhi send nahi, na session chahiye na
OTP. Row bhi nahi likhta - plan ke bytes Telegram par hain hi nahi, isliye use
archive list me daalna "backup ho gaya" jhooth bolna hota.

## Teer (tier ladder)

Har step fail par agli par, aur return me **`tier`** batata hai kaun sa chala:

| # | `tier` | Kya karta hai | Fail par |
|---|---|---|---|
| 1 | `tgup_url` | Asli send | 2 par |
| 2 | `tgup_dry_run` | tgup plan + hash karta hai, **bhejta nahi** - wajah ab saaf hai | 3 par |
| 3 | `unsupported` | Stream ho hi nahi sakta. `use_instead: "link_resolver.resolve_to_local_file"` | caller decide kare |

**Kabhi bhi chup-chaap download nahi.** Tier 2 isi liye hai: pehla send fail hone
par reason PREDICTED nahi, MEASURED hoti hai - aur uske liye session ya OTP
nahi lagta.

Kill switches: **`TDUBBER_DIRECT_ARCHIVE`** (default on),
**`TDUBBER_DIRECT_ARCHIVE_CHANNEL`** (channel force karta hai).

## Do bugs jo is kaam ne pakde

1. **`upload_url_via_go` kabhi result nahi de sakta tha.** `tgup_bridge` jab tak
   `--result-out` na maange, tgup result JSON **likhta hi nahi** - aur phir
   `_result_from()` har successful run ko bhi *"tgup exited 0 without a result
   file"* bol deta tha. **MEASURED** (real binary, range origin): `rc 0`,
   `dry run: 1 parts planned, 195.31 KB hashed` - par Python ke paas kuch
   nahi. Fix: `go_planner.py` ek private temp result file banata hai, padhta
   hai, turant delete karta hai. Ye video nahi hai - kuch hundred bytes ka
   metadata - to zero-disk ka vaada nahi toota.
   **Isi wajah se digest aata hai, aur isi wajah se Tier-0 dedup kaam karta hai.**
2. **Dry run par session gate.** `tgup_bridge.upload()` bina session ke spawn
   hi nahi hota tha - `session_refusal()` - **chahe dry run ho**. Wo refusal
   OTP bachane ke liye hai; dry run OTP kharch hi nahi kar sakta. Par us gate
   ki wajah se zero-disk path ka "bina session ke wajah batao" wala step
   un-authorised machine par **unreachable** tha - yaani zyadatar machines par.
   Ab `dry_run` par gate nahi lagta.

## Commands

```powershell
# 0 disk par link check karo (plan + digest, kuch send nahi)
python -c "import direct_archive,json; print(json.dumps(direct_archive.archive_link('https://youtu.be/XXXX', channel='', api_id=1, dry_run=True), indent=2))"

# Asli archive (session chahiye)
python -c "import direct_archive,json; print(json.dumps(direct_archive.archive_link('https://youtu.be/XXXX', channel='@mychannel', api_id=35578684, api_hash='...'), indent=2))"

# tests
python -m pytest test_direct_archive.py -q     # 18 passed
python -m pytest -q                            # 137 passed
```

## MEASURED (aaj ka apna run, 2026-10-09)

| Check | Result |
|---|---|
| `python -m pytest test_direct_archive.py -q` | **18 passed** |
| `python -m pytest -q` (root suite) | **137 passed** (pehle 120) |
| Prod `projects` rows | 9 -> **9** (delta 0) |
| Prod `telegram_archives` rows | 17 -> **17** (delta 0) |
| Real tgup dry run, range origin, 200000 B | **ok**, digest = payload ka SHA-256, `1 parts planned` |
| 9 MB payload pe origin se range requests | **2 requests** (HEAD + 1 range) |
| Temp me `tgup_result_*` residue | **0** |
| yt-dlp page-URL shapes (single / muxed / playlist / no-url) | 4/4 sahi, `download=False` |
| Range **nahi** deta origin | `direct=False`, reason me "Range" + "downloading", size 0 |

## Abhi bhi PREDICTED

1. **Asli Telegram send** - is path se **koi real upload nahi kiya gaya**. Uski
   wajah **technical nahi hai**: session authorized nahi hai aur OTP type karne
   wala koi console nahi hai. Isliye 9.6 bhi 9.5 ki hi baat dohra raha hai -
   **plan, digest, SQLite row aur refusal behaviour MEASURED hain; bytes ke
   channel me pahunchne ka nahi.** Ye do alag cheezein hain.
2. **App me wired nahi hai** - `app.py` jaan-boojh kar chheda gaya (doosra
   agent). `direct_archive.archive_link()` abhi sirf callable hai, koi button
   ya Drive-tab usse nahi jodta. 9.5 ki "sirf archive karo, dub mat karo" mode
   ka product decision abhi bhi khula hai - **ye usi ka jawab hai**.
3. **YouTube ke signed URLs expire hote hain.** Resume ke dauraan token rotate
   ho jaye to range requests fail honge. 9.5 ka point 4 abhi bhi khula hai.
4. **Muxed renditions stream nahi ho sakte** - alag video + audio ko merge
   karna padta hai, aur merge kaam local file par hota hai. Ye **jaan-boojh
   kar refuse** kiya gaya hai (reason batake), download karke nahi.
5. **Signed URL redacted fingerprint nahi hai** - fingerprint bytes ka hai, URL
   ka nahi, isliye expiry par bhi dedup sahi rehta hai. Ye accha hai, lekin
   `tgup` resume apne aap nahi pakadta (9.5 point 4).

## Jaan-boojh kar nahi kiya

* `app.py`, `pipeline.py`, `telegram_uploader.py`, `cmd/`, `edge/`,
  `mazinger/`, `references/`, `projects/` - chhua hi nahi (doosra agent Kaggle
  notebooks par hai).
* **app-level switch** (`TDUBBER_DIRECT` / `TDUBBER_DIRECT_TIER`) - abhi bhi
  `app.py` me 0 hai. `direct_archive` apne alag switch rakhta hai.
* **Silent disk fallback** - jo is feature ki definition ke khilaf hai.
* **`resolve_to_stream` pe loopback block** - production me `127.0.0.1` jaan
  boojh kar block hai aur wahi rahega. Test sirf apne hi loopback origin ke
  liye guard ko stand-down karta hai, aur wahi ek exception hai.
