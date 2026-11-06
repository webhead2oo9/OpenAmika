# Tools: agents and MCP servers as her hands

OpenAmika is an **MCP client**. Any server speaking the
[Model Context Protocol](https://modelcontextprotocol.io) — stdio command or streamable-HTTP URL —
can give her hands. Personal task agents you already run (**Hermes Agent**, **nanoMuse**) plug in
through `servers/openamika-agents`, which shows every agent to her as the same four tools:
`ask / status / answer / stop`. Configuration lives in `config/tools.json` (gitignored; template:
`config/tools.example.json`), fully editable from the Tools page. Enabling a server puts it into her
context; disabling takes it out. While a task agent is switched on, MekaHime's paper doll stands
beside her on the Use page and plays the agent's side of the exchange.

## Her helpers: set up Hermes Agent and nanoMuse

This is the whole path from nothing to a helper that works for her, on Windows in PowerShell. You need
OpenAmika set up and started once (README, "Set her up") and an [OpenRouter](https://openrouter.ai) key for
the helper's own model; the key she already uses (in `brain\.env`) works, and keeps everything on one bill.
Set up one helper or both: she hands a task to the one she names, or the one you name.

```
 you ── speak / type ──► Amika (brain) ── she answers first; then the clerk turns what she agreed to
                                      │    into hermes__ask / nanomuse__ask (quoting your words)
                                      ▼
            servers/openamika-agents (MCP over stdio, started by the brain from its own folder)
                 │ HTTP + API key                          │ HTTP + token
                 ▼                                         ▼
     Hermes Agent  127.0.0.1:8642              nanoMuse  127.0.0.1:8787
     (%LOCALAPPDATA%\hermes)                   (Nanomuse\ in the OpenAmika folder)
                 │                                         │
                 └──── progress · questions · approvals · result, polled by the brain ────┘
                                      ▼
     Use page: her speech bubble ◄──── the tool dialogue ────► the paper doll's speech bubble
               (she types the task, relays questions, tells you the result)   (avatar/models/Host)
```

Hermes and nanoMuse never talk to you, and never know the paper doll exists: the brain relays everything
through her, and the Use page draws the exchange on her stage.

### Set up Hermes Agent

[Hermes Agent](https://hermes-agent.nousresearch.com) (Nous Research) is a general computer-using agent: web
research, a browser, a terminal, files. Tested with Hermes Agent 0.22.4 and 0.22.7.

1. **Install it** from a normal PowerShell window (not as administrator):

   ```powershell
   & ([scriptblock]::Create((irm https://hermes-agent.nousresearch.com/install.ps1))) -NonInteractive
   ```

   It installs into `%LOCALAPPDATA%\hermes` (its code, its own Python, Node and a browser it drives, Chrome for
   Testing) and puts the `hermes` command on your PATH. Open a new PowerShell window afterwards. `-NonInteractive`
   skips the two parts of the installer that ask questions: its setup wizard (the next steps cover what OpenAmika
   needs) and its gateway service. Installing with Hermes's own one-liner, `iex (irm …/install.ps1)`, instead? Then
   answer **n** when it asks "Start the gateway now after install?": a gateway started now has no API server yet,
   and OpenAmika starts it later, with its key. "Start the gateway automatically on Windows login" is up to you. If
   your antivirus quarantines `uv.exe`, see the troubleshooting part of Hermes's README.
2. **Give it a model:** run `hermes model`, choose OpenRouter and paste your OpenRouter key. The model OpenAmika
   was tested with, `z-ai/glm-5`, is not in its list: choose **Enter custom model name** and type `z-ai/glm-5`.
3. **Turn on its API server, with a key.** OpenAmika reaches Hermes through it, and it opens only with a key of
   16 characters or more (with a shorter one, or a placeholder, Hermes runs without it and says nothing about it):

   ```powershell
   $key = [guid]::NewGuid().ToString('N')       # 32 random characters
   hermes config set API_SERVER_ENABLED true
   hermes config set API_SERVER_KEY $key
   hermes gateway stop                           # a Hermes already running reads the key only when it starts again
   ```

   Both settings go into `%LOCALAPPDATA%\hermes\.env`. You can also add `API_SERVER_ENABLED=true` and
   `API_SERVER_KEY=<your key>` to that file in Notepad. Hermes reads that file only when it starts: the last line
   stops a gateway that was already running without the key (one the installer started, or your own), and OpenAmika
   starts it again with it. With none running it says `No gateway running for this profile`, which is fine.
4. **Optional, to watch it work** in a visible browser window: `hermes config set browser.headed true` and
   `hermes config set AGENT_BROWSER_HEADED true` (on Windows Hermes keeps its browser hidden without the second).
   To try it by hand: `hermes gateway` in one window, then `Invoke-RestMethod http://127.0.0.1:8642/health` in
   another shows status `ok`. Stop it with Ctrl+C: OpenAmika starts it from now on.
5. **Connect it** (see *Connect them to OpenAmika* below): in `config\tools.json` set the top-level `"enabled"`
   and the `hermes` entry's `"enabled"` to `true`, and paste the key from step 3 as `"HERMES_API_KEY"` (*Connect
   them* has a line that copies it). Its `"autostart"` already points at `%LOCALAPPDATA%\hermes\bin\hermes.exe
   gateway`. Keep `HERMES_PROVIDER` and `HERMES_MODEL` equal to what you chose in step 2: they only price her cost
   estimate.

### Set up nanoMuse

[nanoMuse](https://github.com/nano-muse/nanoMuse) is an open-source personal agent (GPL-3.0-or-later; you install
it yourself, and OpenAmika only talks to it over HTTP). OpenAmika uses its Python runtime, `nanomuse serve`, not
the nanoMuse Desktop app; it is not on PyPI, so it is cloned and installed into its own environment. Tested with
nanoMuse 1.0.9. Run these in PowerShell in the OpenAmika folder (`C:\OpenAmika` in the README); its `Nanomuse\`
folder is git-ignored:

```powershell
git clone --depth 1 https://github.com/nano-muse/nanoMuse.git Nanomuse\repo
py -3.11 -m venv Nanomuse\venv
Nanomuse\venv\Scripts\python -m pip install -e "Nanomuse\repo[browser]"   # about a minute, ~310 MB
Nanomuse\venv\Scripts\python -m playwright install chromium               # its own browser, ~440 MB
mkdir Nanomuse\data
notepad Nanomuse\data\config.toml
```

Save this as `Nanomuse\data\config.toml` (paths with forward slashes: `"C:\Users\you\data"` is not valid TOML, since
`\U` starts an escape there; if your OpenAmika is not in `C:\OpenAmika`, change `data_dir`):

```toml
data_dir = "C:/OpenAmika/Nanomuse/data"

[llm]
provider = "openai"                        # the protocol: OpenRouter speaks it
base_url = "https://openrouter.ai/api/v1"
model    = "z-ai/glm-5"
api_key  = "{{vault:OPENROUTER_API_KEY}}"  # from nanoMuse's own vault, next step

[agent]
instructions = "You are working for Amika, an AI companion, on behalf of her user. She relays your final report in her own words, so keep it short and factual, in the language of the task. If you need something only the person can give, ask one clear question. Never claim you did something you did not do."

[sandbox]
mode = "off"            # Windows has no sandbox for it

[browser]
enabled  = true
headless = false        # a visible window, so you can watch it

[cloud]
required = false        # no nanoMuse Cloud account
sync     = false
models   = false

[hub]
enabled = false

[server]
host = "127.0.0.1"
port = 8787
approval_timeout = 600  # shorter than the bridge's 15-minute job limit
```

Then put your OpenRouter key into nanoMuse's vault, so it has its key whoever starts it (the launcher, the brain or
you):

```powershell
Nanomuse\venv\Scripts\nanomuse vault set OPENROUTER_API_KEY --config Nanomuse\data\config.toml
```

Paste the key at the hidden prompt. A yellow "No model yet" line before it is expected (the vault is still empty
at that moment). Do not use `api_key = "${OPENROUTER_API_KEY}"` instead: the launcher starts nanoMuse before the
brain and never passes `brain\.env` on to it, so nanoMuse would start without a key.

To try it by hand: `Nanomuse\venv\Scripts\nanomuse serve --config Nanomuse\data\config.toml --no-qr --host
127.0.0.1 --port 8787` prints `nanoMuse … is ready.` and writes `Nanomuse\data\server_token` on its first start;
stop it with Ctrl+C. The link it prints holds its access token: never share it.

Connect it: in `config\tools.json` set the top-level `"enabled"` and the `nanomuse` entry's `"enabled"` to
`true`. Its paths already point at `C:/OpenAmika/Nanomuse` (change `C:/OpenAmika` in its four paths if your
folder is elsewhere), and set `NANOMUSE_LANGUAGE` to the language you speak with her (`en`, `ja` or `zh-CN`).

### Connect them to OpenAmika

`config\tools.json` holds the helpers (it is git-ignored: your keys stay on your PC). Start it from the example,
once:

```powershell
Copy-Item config\tools.example.json config\tools.json
notepad config\tools.json
```

The example's `hermes` and `nanomuse` entries are complete; you only change what the steps above say:

```json
{
  "enabled": true,
  "mcpServers": {
    "hermes": {
      "command": "python", "args": ["-m", "openamika_agents", "hermes"], "cwd": "../servers/openamika-agents",
      "autostart": {"command": "%LOCALAPPDATA%/hermes/bin/hermes.exe", "args": ["gateway"], "cwd": "%LOCALAPPDATA%/hermes"},
      "env": {"HERMES_URL": "http://127.0.0.1:8642", "HERMES_API_KEY": "PASTE-THE-API_SERVER_KEY-FROM-HERMES-.env", "…": "…"},
      "enabled": true, "kind": "agent", "persona_note": "…"
    },
    "nanomuse": {"…": "…", "enabled": true}
  }
}
```

For Hermes, replace `PASTE-THE-API_SERVER_KEY-FROM-HERMES-.env` with its key. This line copies it from Hermes's
`.env`; run it right before you paste (Ctrl+V), since setting up nanoMuse may have put your OpenRouter key on the
clipboard in between:

```powershell
(Select-String -Path "$env:LOCALAPPDATA\hermes\.env" -Pattern '^API_SERVER_KEY=(.+)').Matches[0].Groups[1].Value | Set-Clipboard
```

(Or open `%LOCALAPPDATA%\hermes\.env` in Notepad and copy what follows `API_SERVER_KEY=`.)

- `command: "python"` is the brain's own interpreter; the bridge runs from `servers/openamika-agents` and needs
  nothing installed (no `pip install`).
- `autostart` is the helper's own start command: the launcher starts it before her brain (its output goes to
  `logs\agent-<name>.log`), and the brain starts it again whenever it finds it down. `%VARIABLES%` work in its
  command, args and cwd, and a relative command or cwd is taken from the OpenAmika folder.
- `kind: "agent"` is what makes it a helper (and what seats the paper doll). `persona_note` is what she is told
  about it, in character; `{user_name}` becomes your name.
- Already have a `config\tools.json` (for the smart home, say)? Copy only the two entries into its
  `mcpServers`. Or open the Tools page, switch to **Professional** (top right), open **Raw config
  (tools.json)**, edit and press **Save raw config**. In Simple mode the Tools page shows the servers' switches
  and Start / Test buttons, but adding a server or a key needs Professional mode or the file.

Then start her: `.\Start_OpenAmika.bat`. If she is already running, saving is enough: the brain reads the file
within 30 seconds and starts the helper itself. On the Tools page, **Tools enabled** and the helper's card switch
must both be on; the card turns green with the helper's version, for example `agent online · v0.22.7` (nanoMuse:
`v1.0.9`), and **Test** says `hermes: 5 tool(s) discovered (agent bridge) · agent online v0.22.7.`.
`envs\brain\Scripts\python.exe launch.py --check` shows `OK agent hermes  online at http://127.0.0.1:8642`,
`OK hermes key  HERMES_API_KEY is the API_SERVER_KEY in …` and `OK paper doll  Host · 64 motions — stands in for
her helpers on the Use page`.

### Meet the paper doll

The paper doll is MekaHime's folded-paper shrine doll (a katashiro, with the MekaHime mark on a fold): the Live2D
model in `avatar/models/Host/`, which `scripts/setup_tts.py` installs together with her own model (summary row
`OK paper doll Host`; its `banks.json` says `"role": "agent"`). It stands in for whichever helper is working.
What connects a helper to it is the helper's entry: **Tools enabled** on and any entry with `"kind": "agent"`
switched on. Then the Use page seats the doll at the bottom right of her stage (within a couple of seconds, even
before the helper is up) and slides it away when no helper is switched on.

Ask her, for example, "Can you have Hermes check tomorrow's weather here?". She answers first, as always. When
she has finished speaking, her command types out in her speech bubble, and from then on the doll plays the
helper's side:

| what happens | the paper doll |
|---|---|
| a helper is switched on / none is | slides in at the bottom right / slides away |
| she types her command to it, or answers it for you | listens (`listen*`) while her bubble is up, and the two turn to each other |
| the helper works | thinks (`think*`) until the job ends |
| a step or a note from the helper | talks (`talk*`), the text in its own bubble, labelled e.g. `hermes → Amika · working` |
| the helper has a question for you, or wants your approval | surprised (`surprise*`) |
| the result | happy (`happy*`); she then tells you the result in her own words |
| an error, or the job was stopped | worried (`worried*`) |
| nothing going on | idle (`idle*`) |

Drag the doll to move it and use the mouse wheel to resize it (kept per browser); **Re-center** puts it back.
Without a paper doll the helpers still work, but they have no stand-in and no speech bubbles: you hear her relays,
and the Use page's **Her helpers** card (and, in Professional mode, the **Tool dialogue** card) shows the rest.
Your own doll: any Cubism model folder in `avatar/models/` whose `banks.json` says `"role": "agent"`, with motions
named by those prefixes (`avatar/models/README.md`).

### Approvals and questions

Before a risky step a helper asks (Hermes by its approval policy, `approvals.mode`, `smart` by default; nanoMuse
by its Sentinel: a shell command always asks, and on Windows so does every Python script). She relays the request.
Say yes or no out loud (your own words are quote-checked; she cannot approve anything herself), or click
**Approve** / **Deny** on the Use page's **Her helpers** card, which also has **Answer** for a question and
**Stop** for a job. An approval nobody answers ends the job when its 15-minute limit runs out (nanoMuse itself
denies it after 10 minutes, its `approval_timeout`); a question still open after the helper's run has ended is
dropped after 30 minutes.

### Stopping

`Stop_OpenAmika.bat` (or Ctrl+C in her console) stops her helpers too: the ones the launcher started, and the ones
the brain started again while it ran. Only a helper the brain started before the brain itself was restarted keeps
running (it was started detached, so it survives a brain restart), and so does a Hermes that was already running
before OpenAmika started (she uses it as it is): close it with `hermes gateway stop`, or end `hermes.exe` /
`nanomuse.exe` in Task Manager.

### When it does not work

Start with `envs\brain\Scripts\python.exe launch.py --check`, then the helper's card on the Tools page; the lines
below are what they say.

| you see | why | what to do |
|---|---|---|
| card red, `agent unreachable`: `Hermes: nothing answers at http://127.0.0.1:8642/health …`, or `start failed`: `hermes did not come up after 3 start attempts …` | Hermes is not running, or it runs without its API server (no `API_SERVER_KEY`, or one under 16 characters or a placeholder) | `launch.py --check` shows `-- hermes key  API_SERVER_KEY in … is missing, shorter than 16 characters or a placeholder …`, and Hermes's own log `%LOCALAPPDATA%\hermes\logs\gateway.log` has no `API server listening on http://127.0.0.1:8642` line. Do step 3; a gateway that is already running reads its key only when it starts, so stop it (`hermes gateway stop`, or end `hermes.exe` in Task Manager) and press **Start** on the card |
| `Hermes is running but rejects the API key (HERMES_API_KEY in config/tools.json must equal API_SERVER_KEY …)`, or `-- hermes key  tools.json HERMES_API_KEY differs from API_SERVER_KEY in …` | the two keys differ | paste the `API_SERVER_KEY` from `%LOCALAPPDATA%\hermes\.env` into `HERMES_API_KEY` |
| `-- hermes key  HERMES_API_KEY in config/tools.json is not set yet …` | the example's placeholder is still there | paste the key |
| `!! agent hermes  not running, and its start command does not exist: …` (or `agent nanomuse`) | not installed where `autostart` says | install it, or fix `autostart` in `config\tools.json` |
| `start failed`: `… exited with code 0` (or `1`) `— see logs/agent-hermes.log`, and that log says `The host gateway already serves profile 'default' — nothing to start` or `A gateway is already running` | a Hermes gateway was already running without its API server (started before step 3, for one by the installer's "Start the gateway now after install?"), so the one OpenAmika starts leaves at once | `hermes gateway stop`, then **Start** on the card (or start her again) |
| `start failed`: `… exited with code N — see logs/agent-hermes.log` | it started and stopped | read `logs\agent-<name>.log` (and Hermes's `logs\gateway.log` / `errors.log`) |
| card green, but every task fails and she says Hermes could not do it | Hermes has no working model | run `hermes model` again, then `hermes doctor` |
| `agent refusing tasks`: `Hermes … is running but refused the last task (Hermes refused POST /v1/runs: HTTP 429 …)` | busy, shutting down, or the session belongs to another client | wait; a task it takes clears it (or 10 minutes) |
| a note `Hermes refused the configured session 'openamika' (profile_mismatch …); using session 'openamika-<pc>' …` | a Hermes home copied from another PC | nothing: the bridge continues in a session of this install |
| `Auxiliary Nous client unavailable` in `logs\agent-hermes.log` | Hermes's optional Nous features, unused with OpenRouter | nothing |
| ``nanoMuse: nothing answers at http://127.0.0.1:8787/api/health (…) — is `nanomuse serve` running?`` | not running, or it stopped at once | `logs\agent-nanomuse.log`; exit code 98 = port 8787 is taken, `config.toml is not valid TOML` = backslashes in a path |
| `agent has no model`: `nanoMuse … is running but has no working model …`, or she says nanoMuse failed with `The model provider refused the API key. Check it under Connections.` | nanoMuse has no OpenRouter key, or a wrong one | `nanomuse vault set OPENROUTER_API_KEY --config …` again, and `api_key = "{{vault:OPENROUTER_API_KEY}}"` in its `config.toml`; then end `nanomuse.exe` in Task Manager, and OpenAmika starts it again. A model picked in nanoMuse's own web app (Connections) overrides `config.toml` |
| `nanoMuse is running but rejects the token (…)` | `NANOMUSE_TOKEN_FILE` points at another data folder | point it at `<data_dir>\server_token` |
| `nanoMuse is still starting (…)` / `agent starting…` | it is coming up | wait (up to 90 seconds) |
| two nanoMuse tasks at once come back mixed | the bridge keeps one nanoMuse thread, and a message sent during a run joins that run | give it one task at a time |
| `timed out waiting for Hermes` / `… nanoMuse` | the job ran past 15 minutes, often an approval nobody answered | answer on the Use page; for longer jobs add `--timeout 1800` to the entry's `args` |
| no browser window | the helper's browser runs hidden | Hermes: step 4; nanoMuse: `[browser] headless = false` |
| `-- paper doll  a task agent is switched on, but avatar/models/ has no paper doll …` | the doll is not installed | `envs\brain\Scripts\python.exe scripts\setup_tts.py` |
| `-- paper doll Host  not downloaded: this OpenAmika does not pin the revision …` (setup summary) | this version of OpenAmika cannot download the doll yet | update OpenAmika (`git pull`) and run the setup again later |
| `-- paper doll Host  not copied: … has no live2d/Host/ …` (setup with `--from-dir`) | that copy of her files was made before the doll was added; her voice and model are installed all the same | run the setup without `--from-dir` once (it downloads the doll once this OpenAmika pins its revision), or from a newer copy |
| no doll on her stage although `OK paper doll` | **Tools enabled** or the helper's switch is off, or her own model is missing (the doll is seated beside her) | switch them on; reload the Use page |
| a helper still running after `Stop_OpenAmika` | the brain started it before the brain itself was restarted (detached), or it was running before OpenAmika started | `hermes gateway stop`, or Task Manager |

## She decides, the clerk transcribes

1. **She knows what she has.** Every enabled server is described to her, in character, in a
   `[你手边的工具]` block inside both the surface and the deep prompt (`persona_note` per server, with
   `{user_name}` filled in), together with the rules: only when *you* asked for something this turn
   and *she* is willing; what she agrees to starts running after she has finished speaking, so she
   must not claim it is already done; she may refuse, stall, grumble or bargain; she can never start
   anything you did not ask for.
2. **She answers exactly as always** — surface line first, deep reply after. Nothing runs before she
   has spoken. "Fine, I'll have it looked up" or "Do it yourself" is the decision.
3. **The clerk.** Once the turn's text is final, a small metered call (ledger phase `tools`, on the
   `clerk_role` model — `surface` by default, DeepSeek-class is plenty) reads what you said and what
   she said out loud and outputs only the calls she committed to. Each call must **quote your own
   words** that constitute the request; the brain verifies the quote against your text in code
   (punctuation- and space-insensitive). No quote, no call. Her inner thoughts are reference only.
   Injected turns (tool relays, cues) never run the clerk; an idle *open* turn runs it only against
   your most recent message (see *Health & open turns* below).
4. **Agents work in the background.** `ask` returns a job id at once; the brain polls `status` every
   few seconds. Progress steps go to the UI (`tool_progress` socket event); a question or an
   approval request is relayed through her as an injected turn; the final report is handed to her as
   a `[工具执行记录]` context block and she tells you in her own words. She is the personality relay:
   the agent never talks to you directly.
   A result reaches her two ways at once. It becomes an injected turn, spoken at the first quiet
   moment. Until she has told you, it also sits in her capability block as a fresh, not-yet-heard
   result, so a turn of yours that lands first already knows it. After each turn the clerk reports
   which results she conveyed (`told`); a relay she no longer needs is dropped, and a relay turn you
   talked over (the gateway discards proactive speech when you speak) is queued again until it is
   actually spoken. While a job runs, the block says so with the elapsed time and forbids reporting
   any name, address or number for it. If she still reports a result no helper has delivered, the
   clerk flags it (`fabricated`) and she is told at once to take it back; the real result, when it
   comes, tells her to correct herself.
5. **Approvals belong to you.** When an agent asks for approval (nanoMuse's Sentinel card, Hermes's
   approval policy), she asks you. Only your explicit yes on a later turn (clerk, quote-verified) or
   a click on the Use page (**Her helpers**) releases it. She has no approve path of her own.

## Policies

Agent bridge tools (`ask / status / answer / stop` on a server with `kind: agent`) are **auto** —
their consent is the quote check. Plain MCP tools keep the three policies: **auto** (runs when she
agreed) · **confirm** (parks as *pending*; you approve on the Use page, it runs, she reports) ·
**deny** (hidden from the clerk). Unknown plain tools default by name: `list_*`/`get_*`/`read_*`/
`search_*`/`health`/`status` → auto, everything else → confirm. New servers start **disabled**.

## The agents

- **Hermes Agent** (Nous Research) — `hermes gateway` with its API server on (`API_SERVER_KEY` of 16 or
  more characters in the Hermes home's `.env`, `%LOCALAPPDATA%\hermes\.env` by default; Hermes's docs also set
  `API_SERVER_ENABLED=true`). The bridge uses the Runs API: `POST /v1/runs`, the SSE event stream
  (`tool.started`, `message.interim`, `approval.request`, `run.completed`…), `approval`, `clarify`,
  `stop`. One `HERMES_SESSION` keeps the conversation with the agent continuous across jobs; a session
  that a Hermes home copied from another PC refuses (409 `profile_mismatch`) is replaced once by
  `HERMES_SESSION-<pc>`. A visible browser needs `browser.headed: true` in its `config.yaml` and, on
  Windows, `AGENT_BROWSER_HEADED=true` in its `.env`; after every browser step the bridge brings that
  window to the front (`HERMES_SURFACE_BROWSER=0` leaves it alone).
- **nanoMuse** — `nanomuse serve`. The bridge keeps one thread titled *OpenAmika* (found again if you
  delete it in nanoMuse's own app): `send`, the timeline (`assistant`, `tool`, `approval`, `question`,
  `notice` events), `/api/approvals/{id}`, `stop`. An error notice (its model refused the key, the network
  failed…) ends the job as failed with nanoMuse's own sentence; its health reads `/api/settings` and says
  `no_model` when its model has no working key. Its `nanomuse mcp` server (screen and hands only) can be
  added as a plain MCP server too. Setup: *Set up nanoMuse* above.
- **Smart home** — `servers/openamika-iot/`, an MCP wrapper around mosquitto + zigbee2mqtt (ported
  from the Amika_IoT bridge): `list_devices`, `get_state`, `set_state`, `set_nickname`,
  `activate_scene`, `health`. Routed like any plain server; see its README. Needs the accepted IoT
  safety statement (below).
- **Home Assistant** — its MCP Server integration URL + a long-lived token. Counts as IoT too.
- **OpenClaw** — `openclaw mcp serve` exposes channel conversations (read/send messages), not its
  toolset; add it as a plain server if that is what you want.

Details and the bridge's environment variables: `servers/openamika-agents/README.md`.

## Safety and responsibility (IoT)

Smart-home servers give her hands in the physical world, so they come with a statement you must
read and accept first: [`docs/IOT_DISCLAIMER.md`](IOT_DISCLAIMER.md) (English, 日本語, 简体中文). In
short: she runs on non-deterministic language models; she can mishear, misunderstand, act on the
wrong device or at the wrong time, repeat or skip an action, or be manipulated by what she reads or
hears (prompt injection); models, providers, networks, bridges, Zigbee/MQTT and firmware can fail.
There is no guarantee that she will not cause harm to your devices, property, data, people or
animals. Everything you connect is at your own risk, and MekaHime Inc. and the contributors are not
responsible for any outcome, damage, loss or injury, to the maximum extent the law allows (the
software is provided as is, MIT License). Never connect anything that could cause danger if misused
or abused (heaters and other heating appliances, valves and pumps, locks, garage doors and gates,
alarms, medical, child- or pet-safety equipment, vehicles, machinery, high-power loads). The quote
check, the policies and the `confirm` step are best-effort, never a guarantee; keep manual, physical
control of every device, and turn IoT off at any time on the Tools page.

How it is enforced: a smart-home server is the `openamika-iot` bridge (named `iot` or started as
`openamika_iot`), Home Assistant's MCP server (recognised by its name or its URL), or any entry with
`"kind": "iot"`. Turning one on from the Tools page shows the statement in a dialog: an unticked
"I have read and accept this" box and a separate **Enable IoT** button; Cancel leaves IoT off. The
acceptance is stored in `config/tools.json` as `"iot_disclaimer": {"version", "accepted_at"}` (only
`POST /api/build_f/iot/disclaimer` writes it; a config save keeps what is on disk). Until the current
version is accepted, the gateway and the brain refuse a config that switches a smart-home server on
(409, `iot_disclaimer_required`), the brain's MCP hub refuses to start, connect or call one on every
path (her turns, the health loop, the Smart home panel, Test, approvals), the launcher holds
mosquitto and zigbee2mqtt back (and starts them as soon as you accept), and the bridge itself
refuses to start without the accepted version (`OPENAMIKA_IOT_DISCLAIMER_ACCEPTED`, set by the
brain, or `--accept-iot-disclaimer <version>` when you run it yourself). An install that already had
IoT on keeps it off, and the Tools page says why; when the statement changes, its version
(`IOT_DISCLAIMER_VERSION` in `brain/iot_disclaimer.py`) changes, and the dialog asks again.

## Health & open turns

Every agent bridge also exposes `health` (is the agent process up, does it accept our key, can it work?).
The brain probes it every 30 s in the background and again right before every `ask`. The server card on
the Tools page shows it (green = bridge up and agent online, red = bridge up but the agent unreachable,
refusing tasks or without a model, amber = bridge not up, with the reason); her capability block says
*联系不上* with the reason so she does not promise what cannot run; and an `ask` to an unreachable agent
is **not sent** — she tells you the helper is down instead of promising and failing later. A job that dies
of a connection error is relayed in plain words ("联系不上 hermes", not a socket error) and marks the agent
unreachable until the next probe; a job the agent itself declined (an HTTP refusal, a model that refused
its key) is relayed with its reason. The block also says when nothing is running, so she cannot believe a
helper is "still on it".

**Starting the agents for you.** An agent entry may carry `autostart`, its own start command (a
string, quotes around a path with spaces, or `{command, args, cwd, env}`; the Tools page has a *Start
command* field on every agent card in Professional mode). `%VARIABLES%` in its command, args and cwd are
expanded, and a relative command or cwd is taken from the OpenAmika folder, by the launcher and the brain
alike. The launcher starts
it before her brain and waits for its health URL (`HERMES_URL/health`, `NANOMUSE_URL/api/health`). When a
probe finds the agent unreachable, the brain launches that command detached from itself (so it survives a
brain restart), the card shows *agent starting…* for up to 90 s, and the clerk waits a moment for a
just-launched agent before giving up on an ask. After three failed attempts the card shows *start failed*
with the exit code; the output is in `logs/agent-<name>.log` and the **Start** button tries again. With no
`autostart`, enabling only connects the bridge and the card stays red until you start the agent yourself.

She may also agree on one of her own turns (an idle *open* turn right after your request: "fine,
having Hermes look it up"). The clerk then runs with your most recent message (within 10 minutes) as
the only source of quotes, at most once per message, and never takes approvals or answers from such a
turn. The thinking layer, which writes those open turns, sees a third-person copy of the block
(`[她手边的工具]`) so it does not script results that do not exist.

## The tool dialogue on the Use page

The Use page shows the jobs in flight and anything waiting for your yes on its **Her helpers** card (Approve,
Deny, Answer, Stop). In Professional mode, the **Tool dialogue** card under her stage shows a transcript of
her side of every delegation and the helper's side: what she asked (with the words of yours that authorised
it), each step the agent reports, what it says back, its questions and approval requests, your answers
(spoken or clicked), the final result or failure, and the brain's own notes (a request that was not sent
because the agent was down, a result she invented and was told to retract). It is the `dialogue` list of
`/api/build_f/tools`, polled every couple of seconds while you are connected. Hermes's headed browser is
also brought to the front with the page it is on after every browser step, so you can watch it work.

### On stage: the paper doll, speech bubbles, her loading emote

The same exchange plays out on her stage. A second Live2D model marked `role: "agent"` in its
`banks.json` (see `avatar/models/README.md`; MekaHime's paper doll, `avatar/models/Host/`, comes with
`scripts/setup_tts.py`) stands in for whichever helper is working: the first such model, in folder-name
order. It is seated bottom-right whenever the tool layer is on and an agent server is enabled (healthy or
not), and slides away when none is. Each new line of the tool dialogue becomes a speech bubble: hers out of
her, the helper's out of the doll, and the two turn to look at each other while a bubble is up. The
bridge's own steps (`sent`, `started`, `stop requested`, `approval granted`…, marked `internal`) stay in the
transcript only: the doll says what the helper itself said or did. The
doll plays motions by state (table in *Meet the paper doll*): `talk*` while it has the word, `listen*`
while she talks to it, `think*` while a job is running, `happy*` on a result, `worried*` on an error or a
stop, `surprise*` when it has a question for you, `idle*` otherwise. Only its own state loop picks them: the
Live2D library's random idle motions are switched off on the doll. The paper doll's set is retargeted from her own
recordings with `avatar/retarget_motions.py --preset agent`. Lines that were already in the log when the
page opened do not get a bubble; the transcript keeps everything. You can drag or zoom the doll like
her; "Re-center" resets both.

Her animated "loading" emote (the model's `Loading` expression plus the loop named by
`busy_motion` in her `banks.json`, `Scene1` on the bundled model) is layered over whatever face she
is making while she is waiting on herself: from the moment the
surface tier hands a turn to the deep layer (`say+think` / `think`) until the deep's first sentence
starts playing (the gateway tags each `tts_audio` with its tier). It switches off on the deep's first
words, at the end of the turn, on a barge-in or a new turn. A helper still working is not her
waiting: that shows on the doll (its `think*` motions), never as her icon.

## Costs & privacy

The clerk is one small call per user turn while any server is enabled (phase `tools` in the ledger /
Use page); job polling is local. What a helper spends on its own model is booked too when it reports
it: Hermes returns token totals per run, and the bridge hands them to the brain, which records a
receipt under `agent:hermes`. OpenRouter did not confirm that charge, so the ledger keeps it as an
*estimate* at the model's list price (`HERMES_MODEL`; cached input is not discounted), separate from
the confirmed figure. The Use page's session cost shows confirmed plus estimated and says how much is
estimated. In MekaHime's own use one Hermes task took 55,000 to 740,000 tokens, about $0.03 to $0.45 on
`z-ai/glm-5`. nanoMuse exposes no usage through its API, so its spend is not in the ledger; the
OpenRouter key's own usage counter remains the only complete number. What an agent brings back (a web page summary, an email) enters her
context for the relay and may be remembered like anything she said. The Tools page shows exactly
what is enabled, every call with the words of yours that authorised it, and every job; the master
switch removes everything from her context. `config/tools.json` holds the helpers' keys in plain text
on your PC. The Tools page's raw editor masks values in `env`, `headers` and `autostart.env` whose names
look like keys, tokens or passwords (leave the mask in place to keep a key, or type a new one); a token
written into a `url` or `args` stays visible. Its pages answer only this PC's browser, or with
`Tailscale_Serve_OpenAmika.bat` every device on your tailnet, which can then change the helpers' start commands.

## Requirements

`pip install "mcp>=1.2,<2"` in the **brain** environment (in `brain/requirements.txt`; `scripts/setup_core.py`
installs it). The agents bridge needs nothing more: the brain runs it from `servers/openamika-agents` with its
own interpreter, and `httpx` comes with `mcp` (`pip install -e servers/openamika-agents` is only for running it
elsewhere). Hermes Agent or nanoMuse themselves are installed as above; `pip install -e servers/openamika-iot`
for the smart home.
