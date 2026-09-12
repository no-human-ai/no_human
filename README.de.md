<div align="center">

<img src="docs/assets/nh-mark.png" alt="" width="140" height="140">

# no_human

<!-- mcp-name: io.github.no-human-ai/no_human -->

**Vom Ticket zum geprüften Pull Request.**<br>***Kostenlos und Open Source, auf deinem Rechner.***

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · **Deutsch** · [Português (Brasil)](README.pt-BR.md)

[![latest release](https://img.shields.io/github/v/release/no-human-ai/no_human?label=release&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases/latest) [![CI](https://img.shields.io/github/actions/workflow/status/no-human-ai/no_human/ci.yml?branch=main&label=CI)](https://github.com/no-human-ai/no_human/actions/workflows/ci.yml) [![python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/) [![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![downloads](https://img.shields.io/github/downloads/no-human-ai/no_human/total?label=downloads&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases) [![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/mSARvj6yW6)

[getnohuman.com](https://getnohuman.com) · [Schnellstart](docs/quickstart.md) · [Doku](docs/README.md) · [Sieh zu, wie es einen Sprint abarbeitet](https://getnohuman.com/demo) · [Discord](https://discord.gg/mSARvj6yW6)

[![Download für macOS](https://img.shields.io/badge/Download%20f%C3%BCr-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Download für Windows](https://img.shields.io/badge/Download%20f%C3%BCr-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Download für Linux](https://img.shields.io/badge/Download%20f%C3%BCr-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

<a href="https://getnohuman.com/"><img src="docs/assets/hero-loop-poster.jpg" alt="Das no_human-Board: eine Aufgabe wartet in „Needs answer“ auf eine Antwort, vier Aufgaben laufen parallel, ein Pull Request ist bereit für das Review." width="880"></a>

<sub>▶ <a href="https://getnohuman.com/">Sieh dir die Schleife an</a> — ein Ticket rein, ein geprüfter Pull Request raus; die ganze Schleife in 57 Sekunden.</sub>

</div>

> Dieses Dokument ist eine Übersetzung der englischen README. Bei Abweichungen gilt die [englische Fassung](README.md). Die verlinkten Dokumente sind derzeit alle auf Englisch.

Die KI-Coding-Fabrik, der du <ins>**vertrauen kannst**</ins>:

- **Ein Plan vor jeder Zeile Code**, aus dem Ticket und dem, was no_human in
  deinem Repo findet. Scheitert die Planung, wird dem Coder gesagt, dass er
  ohne Plan arbeitet; wird die Änderung als trivial eingestuft, entfällt der
  Plan, ohne dass der Coder davon erfährt — das ist so gewollt, und dass er
  übersprungen wurde, steht trotzdem im Event-Stream des Laufs.
- **Ein Review, das dagegenhält.** Ein anderes Modell, in einer Session, die
  das Transkript des Coders nie gesehen hat, mit dem Auftrag, „fertig“ zu
  widerlegen. Du bekommst eine Pass/Fail-Checkliste, die Datei und Zeile
  nennt — nie eine numerische Selbstbewertung.
- **Ein Manipulationsschutz.** Gelöschte Tests, neue Skips, eine Assertion,
  die zur Tautologie umgeschrieben wurde — maschinell gezählt, bevor das
  Review-Gate läuft, und danach anhand deiner Akzeptanzkriterien gerechtfertigt;
  sonst bricht der Versuch ab.
- **Der Beweis, dass der Fix den Bug behoben hat.** Die als Beleg vorgelegten
  Tests müssen an der Merge-Base fehlschlagen und auf dem neuen Stand
  durchlaufen — das Reproduktions-Gate führt beides aus. Ab Werk gilt das für
  Python-Bugfixes; mit `repro_gate.mode: required` gilt es für jede
  Aufgabenart und jede Änderung.
- **Deine Tests laufen**, lokal und auf Wunsch über deine CI — und in einem
  PR, für den kein Testkommando gefunden wurde, steht sichtbar **NOT RUN**.
- **Ein ehrlicher Stopp.** Wenn es nicht fertig wird, hält es an und sagt,
  warum — eine konkrete Frage, wenn deine Antwort es weiterbringt, ein
  strukturierter Bericht, wenn schlicht das Budget aufgebraucht ist — nie ein
  erfundener, plausibel aussehender Diff.

## Installation

### Eine Zeile (CLI + Board)

```bash
uv tool install no-human   # oder: pipx install no-human — das Wheel enthält das Board
nh init && nh doctor       # Token, Konfiguration, erstes Repo; dann belegen, dass die Installation trägt
```

### Desktop-App

[![Download für macOS](https://img.shields.io/badge/Download%20f%C3%BCr-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Download für Windows](https://img.shields.io/badge/Download%20f%C3%BCr-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Download für Linux](https://img.shields.io/badge/Download%20f%C3%BCr-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

Jedes Release liefert neben dem Artefakt eine SHA-256-Prüfsumme mit.
Plattformhinweise und der Ablauf beim ersten Start:
[docs/quickstart.md](docs/quickstart.md).

### Aus dem Quellcode

```bash
git clone https://github.com/no-human-ai/no_human.git && cd no_human
uv sync                 # installiert den `nh`-Einstiegspunkt in .venv
(cd web && npm install && npm run build)   # baut das Board (die erste Installation kann Minuten dauern)
uv run nh init          # Token, Konfiguration, erstes Repo (etwa 2 Minuten)
uv run nh doctor        # prüfen, dass die Installation trägt, bevor du dich darauf verlässt
```

Der `web`-Build ist nicht optional, wenn du das Board willst: Ein
Quellcode-Checkout enthält kein `web/dist`; ohne diesen Build liefert `nh start`
also nur die API aus und rendert keine Oberfläche. Vorausgesetzt werden
Python 3.12+, [uv](https://github.com/astral-sh/uv), git und — für den
Board-Build — Node mit npm.

## Produkt-Highlights

<table>
  <tr>
    <td width="36%" valign="middle">
      <h3>Ein Plan vor jeder Zeile Code</h3>
      <p>Akzeptanzkriterien, die du nachprüfen kannst — erstellt aus dem Ticket und deinem Repo.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-plan.png" alt="Der Plan der Aufgabe: das Verständnis der Aufgabe als drei Akzeptanzkriterien, die zwei zu ändernden Dateien, der Ansatz, der Testplan, was außerhalb des Scopes liegt, und das Verifikationskommando." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Ein unabhängiger Reviewer</h3>
      <p>Ein zweites Modell, das die Session des Coders nie gesehen hat, mit dem Auftrag, „fertig“ zu widerlegen. Bestanden oder nicht; jeder blockierende Befund nennt Datei und Zeile.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-verdict.png" alt="Das Urteil des Reviewers: PASSED, jedes Akzeptanzkriterium abgehakt mit der Datei und Zeile, die es erfüllt, dazu eine nicht blockierende Kleinigkeit mit dem Diff, auf den sie zeigt." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Deine Tests, direkt im PR</h3>
      <p>Laufen lokal oder über deine CI. Wurde kein Testkommando gefunden, steht dort <b>NOT RUN</b>, nie ein leeres Feld.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tests.png" alt="Das Panel „Test results“ der Aufgabe: CLEAN, 5 von 5 bestanden, darunter die pytest-Ausgabe." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Ein Manipulationsschutz</h3>
      <p>Gelöschte Tests, neue Skips und tautologische Assertions werden vor dem Review gezählt. Ohne Rechtfertigung bricht der Versuch ab.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tamper.png" alt="Ein abgebrochener Versuch: ein rotes TAMPER-DETECTED-Banner, das Reviewer-Urteil FAILED und der blockierende Befund, dass drei Tests gelöscht wurden, ohne dass ein Akzeptanzkriterium das rechtfertigt." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Der Beweis, dass der Fix den Bug behoben hat</h3>
      <p>Die als Beleg vorgelegten Tests müssen auf dem alten Code fehlschlagen und auf dem neuen durchlaufen. Das Gate führt beides aus, und das Ereignisprotokoll zeigt das Urteil.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-repro.png" alt="Das Ereignisprotokoll der Aufgabe: Tests bestanden, Status reviewing, die Manipulationsprüfung des Reviewers auf none, das Reproduktions-Gate auf pass, required, dann Lint, Commit und das Öffnen des Pull Requests." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Ein ehrlicher Stopp</h3>
      <p>Wenn es dich braucht, parkt es mit einer konkreten Frage, statt zu raten.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-question.png" alt="Die Spalte „Needs answer“ auf dem Board: eine geparkte Aufgabe mit ihrer Frage „Dedupe by user, or by digest id?“ und ein Button „Answer question“; daneben die Spalten „Working“ und „Review PR“." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Die Tickets deines Trackers, auf deinem Board</h3>
      <p>Wähle Jira- oder Linear-Tickets aus dem Backlog (monday.com-Boards werden gepollt). Jedes wird vor dem Start gemeinsam mit dir abgegrenzt.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-backlog.png" alt="Das aus Jira synchronisierte Backlog: vier passende Tickets ausgewählt, dazu ein Button „Start 4 tasks“." width="100%" />
    </td>
  </tr>
</table>

<sub>Standbilder: das echte Board mit einem Demo-Workload.</sub>

## Eine Aufgabe ausführen

`nh` ohne Argumente startet die Shell: deine Spalten, ein Live-Tail der
Events und ein Eingabefeld, in dem du eine Aufgabe in normaler Sprache
beschreibst. Alle Kommandos unten funktionieren weiterhin.

```bash
nh                                   # die Shell
nh start                             # Board + Worker auf 127.0.0.1:8420
nh task add https://github.com/org/repo/issues/42 --repo ~/git/repo
nh status                            # needs-you / working / waiting / done
nh review <id>                       # die Belegcheckliste des Reviewers
nh diff <id>                         # der Diff, den es ausliefern will
nh approve <id>                      # deine Freigabe mergt den PR per Squash (git.approve_identity)
nh reject <id> --reason "..."        # mit Feedback zurückgeben
```

## Integrationen

Richte no_human auf den Tracker, den du ohnehin benutzt, und es holt die
Tickets auf dein Board — der Filter eines Trackers steht in deiner
Konfiguration, nie im Text einer Aufgabe, und bei einem Transportfehler wird
geloggt und beim nächsten Tick erneut versucht, statt den Pool abstürzen zu
lassen.

| Tracker | Wie die Tickets ankommen | Filter, den du konfigurierst |
|---|---|---|
| **Jira Cloud** | Per REST `search/jql` gepollt (HTTP Basic `email:token`) | `integrations.jira.jql` |
| **Linear** | Per GraphQL-API gepollt | `integrations.linear.team_key` + `state_types` + `label` |
| **monday.com** | Per GraphQL v2 gepollt | `integrations.monday.board_id` + `status_column` + `todo_labels` |

Ist das Write-back aktiv (`write_back`, standardmäßig aus), wandert das
Ticket mit der Aufgabe mit — zugeordnet über Statuskategorie, Typ oder das
Label, das du angibst, nie über eine fest verdrahtete Transition-ID — und
bekommt den PR-Link; eine Aufgabe, die einen Menschen braucht, wird
kommentiert, nie weitergeschoben. GitHub- und
GitLab-Issues lassen sich per URL als Aufgaben importieren, und PRs oder MRs
werden auf deinem eigenen Host geöffnet; Slack und Teams bekommen eine
Nachricht, wenn eine Aufgabe dich braucht; Jenkins und CircleCI können deine
Test-Layer ausführen und als Gate für die Schleife dienen. Die Einrichtung
der einzelnen Integrationen: [docs/adapters.md](docs/adapters.md).

**Sieh dir den Jira-Ablauf von Anfang bis Ende an** — Tickets von einem
Jira-Board synchronisiert, abgegrenzt, umgesetzt und als Pull Request
geliefert, der das Review bestanden hat (klicken für das vollständige Video
mit jedem Schritt):

[![Jira-Ablauf, Demo](https://getnohuman.com/assets/demo-jira.gif)](https://getnohuman.com/assets/demo-jira.mp4)

<p align="center">▶️&nbsp;&nbsp;<strong><a href="https://getnohuman.com/assets/demo-jira.mp4">Die vollständige Demo abspielen</a></strong> — 1:33, vom Jira-Board zum PR, der das Review bestanden hat</p>

## MCP-Server — gib ihm Arbeit aus dem Agenten heraus, in dem du schon bist

no_human bringt einen **MCP-Server (Model Context Protocol)** mit: eine
stdio-Bridge auf Basis des offiziellen Python-MCP-SDK, mit der Claude Code,
Cursor oder jeder andere MCP-Client Arbeit bei deinem lokalen no_human
einreichen und den Stand abfragen kann.

```bash
nh mcp-serve        # der MCP-Server, über stdio
```

Zwei Tools, mehr nicht:

| Tool | Was es tut |
|---|---|
| `task_add(title, description, repo_path)` | Reicht eine Aufgabe ein. no_human plant sie dann, schreibt die Änderung, führt deine Tests aus, lässt sie von einem zweiten Modell reviewen und öffnet den Pull Request. |
| `task_status(task_id_or_external_id)` | Gibt den aktuellen Stand dieser Aufgabe zurück — Status, Versuche und den PR-Link, sobald es einen gibt. |

Er spricht mit deinem eigenen no_human unter `http://127.0.0.1:8420` und mit
sonst nichts: keine Authentifizierung, weil diese Adresse localhost ist, und
kein Dienst von uns dazwischen. Für Claude Code liegt derselbe Server als
Plugin bei — dieses Repository ist sein eigener Plugin-Marketplace, die
beiden Tools erscheinen also in deiner Session nach:

```
/plugin marketplace add no-human-ai/no_human
/plugin install no-human@no-human-ai
```

Jeder andere MCP-Client bekommt den üblichen stdio-Eintrag:

```jsonc
// .mcp.json
{ "mcpServers": { "no_human": { "command": "nh", "args": ["mcp-serve"] } } }
```

## Dokumentation

| | |
|---|---|
| [quickstart.md](docs/quickstart.md) | Von null zur ersten Aufgabe, je Plattform |
| [configuration.md](docs/configuration.md) | Jede Einstellung und ihr Standardwert |
| [verification.md](docs/verification.md) | Die Gates, die begrenzte Schleife, die Grenzen |
| [security.md](docs/security.md) | Auth-Grenze, die Nie-mergen-Regel, die Guards |
| [blockers.md](docs/blockers.md) | Eskalation, Wake-Watcher, `nh reply` |
| [adapters.md](docs/adapters.md) | Intake, Kontext, VCS- und CI-Backends |
| [eval.md](docs/eval.md) | Golden Set, Replay-Scoring, Shadow-Modus |
| [CHANGELOG.md](CHANGELOG.md) | Was sich geändert hat, je Release |

## Entwicklung

```bash
uv sync
uv run pytest -q
uv run nh --help
```

Issues und Pull Requests sind willkommen; führe vor dem Einreichen
`uv run pytest -q` aus.

Wenn dir no_human eine Review-Runde erspart hat — ein Stern hilft anderen,
es zu finden:
[![GitHub stars](https://img.shields.io/github/stars/no-human-ai/no_human?style=social)](https://github.com/no-human-ai/no_human/stargazers)

## Community

Fragen, Fehlerberichte und Läufe, die es wert sind, gezeigt zu werden: Komm
in den [Discord](https://discord.gg/mSARvj6yW6), poste auf
[r/no_human](https://www.reddit.com/r/no_human/) oder öffne ein
[GitHub-Issue](https://github.com/no-human-ai/no_human/issues).

## Lizenz

MIT — siehe [LICENSE](LICENSE). Die Lizenz deckt den Code ab, nicht den
Namen: [TRADEMARK.md](TRADEMARK.md) ist die Richtlinie zur Verwendung von
„no_human“ und des Logos. Wer eine Binärdatei paketiert, übernimmt Pflichten,
die beim reinen Quellcode nicht anfallen; aufgeführt sind sie in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
