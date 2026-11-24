# QT Stems: product plan

Planning only. Nothing here is built. Prices and licence points are rough or second-hand and flagged as such; verify before acting. This is not legal advice.

## 1. Where we are
One owner PC runs the backend (FastAPI + Demucs on the home GPU). Friends get a profile each (own passcode stored as an argon2 hash, own library, sessions and downloads), a 5 GB / 2-job / 200 MB allowance, and reach the PC through Tailscale. It works for about five trusted people. It is not a product yet: access depends on one PC being on, onboarding is manual (invite link plus a passcode sent by hand), and there is no sign-up, billing or recovery.

## 2. Real multi-user accounts
**What a real account system adds over today's passcodes:**
- Sign-up and sign-in by email link or "Sign in with Google/Apple" instead of a passcode the owner generates. Self-service recovery.
- Per-account records in a database (SQLite is enough to start, Postgres later) instead of `users.json` plus an `owner` field in each `meta.json`: user, song, job, usage, plan.
- Sessions: short-lived signed tokens (cookie or bearer) instead of a long-lived header passcode; sign out everywhere; per-device list.
- Roles (user, admin) and an admin page instead of `users.py`: invite, suspend, change quota, see usage.
- Quotas and rate limits stored per account and per plan; usage metering (songs, GPU seconds, storage) so limits can differ by plan.
- An audit log of sign-ins and deletes; account deletion that really removes songs (needed for privacy law, see section 6).

**Suggested route:** do not hand-roll it. Use a hosted auth provider (Clerk, Auth0, Supabase Auth, Firebase Auth; all have free tiers) or a self-hosted library, and keep our own tables for songs and quotas keyed by the provider's user id. Keep the owner passcode as a break-glass admin login.

**Stages:**
1. *Invite-only accounts* (now to about 25 users): email magic link, SQLite, admin page, same home PC. Replaces `users.py`.
2. *Open sign-up with a free tier*: email verification, abuse limits, upload checks for size and type, terms of service accepted at sign-up.
3. *Paid plans*: Stripe checkout, plan limits, receipts, cancel and refund flow.

## 3. Hosting options beyond Tailscale
| Option | What it is | Rough cost | Good for | Problems |
|---|---|---|---|---|
| **A. Home PC + Tailscale (today)** | Your GPU; friends join your tailnet | About $0 extra (electricity, your internet) | Under about 6 friends (Tailscale's free Personal plan allowed 6 users when I checked; verify) | Friends must install Tailscale; the PC must be on; your upload speed limits downloads |
| **B. Home PC + public front door** | Same PC, public HTTPS address (Tailscale Funnel or Cloudflare Tunnel) with real accounts | $0 to about $10/mo (domain) | 10 to 50 light users, nothing for friends to install | Anyone on the internet can reach your home machine: needs accounts, rate limits and monitoring first. One PC means one queue. Funnel is meant for modest traffic. Home ISP terms may not allow serving a public service |
| **C. Cloud web + GPU on demand** | Small always-on web server and database; GPU workers started per job (Modal, RunPod serverless, Replicate, or AWS/GCP spot) | Web and database $5 to $25/mo. GPU roughly $0.40 to $1.50 per hour of T4/L4/A10-class time; a 4-minute song should split in under a minute, so about **1 to 3 cents of compute per song** plus cold starts (estimate; measure it) | A real product with unpredictable load, independent of your PC | More engineering (job queue, storage, secrets); cost scales with use; cold starts add seconds to a minute |
| **D. Cloud web + always-on GPU** | One GPU VM kept running | Roughly $200 to $600+/mo | Steady heavy load only | Pays for idle time; worse than C until thousands of songs a day |
| **E. Hybrid** | Cloud web and storage; jobs go to your home GPU when it is up and spill to a cloud GPU when not | C's fixed costs plus less GPU spend | Cheapest path from friends to product | Most complex; needs a job queue and workers that pull jobs, so the home PC only makes outbound connections |

**Storage** is the big recurring cost. A song is roughly 100 to 300 MB as lossless stems, so 5 GB is only about 20 to 50 songs. Object storage runs very roughly $0.015 to $0.025 per GB-month, and Cloudflare R2 charges no egress. Plan on lossy (MP3/AAC) stems by default, expiry for free users (for example 30 days), and download on demand.

**Recommendation:** stay on A for friends. When people outside your circle want in, go to E or C rather than B: put accounts in front first (stage 1), keep the home PC as one worker, and add a cloud GPU worker only once the queue gets long. Do not expose the home PC directly to the public web.

## 4. Friend onboarding
**Today:** (1) send the Tailscale invite, (2) they install Tailscale, (3) send the URL and `FRIEND_GUIDE.md`, (4) send the passcode through a different channel, (5) they open `/status` if something fails. Lost passcode: `users.py reset <name>`.

**Pain points:** installing a VPN is the biggest drop-off; the passcode is one more secret to lose; the owner is support for everyone; there is no way to see who is stuck.

**Improvements, in order:**
1. An "add a friend" action in an owner page that creates the profile, shows the passcode once, and copies a ready-to-send message (address plus guide).
2. Passkeys or email magic links instead of passcodes (stage 1 accounts).
3. Drop Tailscale for friends by moving to hosting option B or E.
4. A first-run sample song in the app so the first experience works without uploading anything, and an in-app "send diagnostics" button replacing "send me a screenshot".
5. A status page with history, and an alert to the owner when the PC or GPU is unreachable.

## 5. Licences of the separation models and components
Check each again before launch. I worked from public pages and memory, not legal review.
- **Demucs code (Meta / Défossez):** MIT licence per its repository, which allows commercial use of the *code*.
- **Pretrained weights (htdemucs and related):** unclear. Discussion on the maintainer's Hugging Face repo reports that the weights were described as for research use, and third-party notes say no explicit grant for the weights was identified and that they were trained on MUSDB18-HQ plus extra proprietary data. MUSDB18's own terms are research-oriented. **Treat commercial use of the stock weights as unresolved until the maintainers or a lawyer confirm.** Fine for private use among friends; a blocker for charging money.
- **Alternatives to evaluate if the weights stay unclear:** Open-Unmix (MIT code, trained on MUSDB18, so the same data question), Spleeter (MIT code and models, Deezer), and community models (BS-RoFormer, MDX-style) whose licences vary and are often non-commercial or unstated. A safe route may be training or fine-tuning our own model on licensed or self-made data, or asking Meta for written permission.
- **Other components:** PyTorch (BSD-style), FastAPI, uvicorn, argon2-cffi (permissive), numpy and soundfile (BSD), ffmpeg (LGPL or GPL depending on the build; we run it as a separate program, which is the safest pattern, and we should not bundle a GPL build into anything we distribute). Run a licence scan (for example `pip-licenses`) before launch and keep a NOTICE file.
- **Tailscale:** the Personal plan is for non-commercial use, so a paid product cannot rely on it for its customers.

## 6. Terms needed for uploaded audio
Users will upload songs they do not own. Have these in place before open sign-up, drafted by a lawyer; this is only the checklist:
- **Terms of service:** the user confirms they have the right to upload and process each file; we only process it to give them stems; no sharing or public listing of stems; personal, non-commercial practice use unless they hold the rights; we may remove content and close accounts.
- **Licence grant from the user:** a narrow, revocable permission to store and process the file and produce stems for them, nothing more. No training on uploads without separate, explicit opt-in.
- **Copyright handling:** a designated contact and a takedown process (DMCA in the US; other regions differ) and a repeat-infringer policy. Stems are derivative copies of copyrighted recordings, so a service that stores them is more exposed than one that processes and discards. Safer defaults: private-only libraries, no public links, short retention, delete-everything option.
- **Privacy policy and deletion:** what we store (email, files, usage), where, for how long, and a working "delete my account and all songs". Covers GDPR, UK GDPR and CCPA basics; list processors (cloud, auth, payments).
- **Business risk:** rights holders are active against stem-splitting services. Starting private, friends-only and no-redistribution, and getting counsel before charging, is the conservative path.

## 7. Suggested order of work
1. Resolve the weights licence question (contact the maintainers; test an alternative model for quality). Gate everything commercial on this.
2. Stage-1 accounts with a hosted auth provider and an admin page.
3. A job queue with a worker that pulls jobs (home PC first), so a cloud GPU can be added later.
4. Terms, privacy policy and takedown process.
5. Cloud web and storage plus one cloud GPU worker (option E), then open sign-up with a free tier.
6. Payments only after steps 1 to 5.
