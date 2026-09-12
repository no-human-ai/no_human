<div align="center">

<img src="docs/assets/nh-mark.png" alt="" width="140" height="140">

# no_human

<!-- mcp-name: io.github.no-human-ai/no_human -->

**Du ticket à la pull request relue.**<br>***Gratuit et open source, sur votre machine.***

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · **Français** · [Deutsch](README.de.md) · [Português (Brasil)](README.pt-BR.md)

[![latest release](https://img.shields.io/github/v/release/no-human-ai/no_human?label=release&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases/latest) [![CI](https://img.shields.io/github/actions/workflow/status/no-human-ai/no_human/ci.yml?branch=main&label=CI)](https://github.com/no-human-ai/no_human/actions/workflows/ci.yml) [![python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/) [![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![downloads](https://img.shields.io/github/downloads/no-human-ai/no_human/total?label=downloads&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases) [![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/mSARvj6yW6)

[getnohuman.com](https://getnohuman.com) · [Démarrage rapide](docs/quickstart.md) · [Documentation](docs/README.md) · [Voyez-le abattre un sprint entier](https://getnohuman.com/demo) · [Discord](https://discord.gg/mSARvj6yW6)

[![Télécharger pour macOS](https://img.shields.io/badge/T%C3%A9l%C3%A9charger%20pour-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Télécharger pour Windows](https://img.shields.io/badge/T%C3%A9l%C3%A9charger%20pour-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Télécharger pour Linux](https://img.shields.io/badge/T%C3%A9l%C3%A9charger%20pour-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

<a href="https://getnohuman.com/"><img src="docs/assets/hero-loop-poster.jpg" alt="Le tableau no_human : une tâche en attente d'une réponse dans Needs answer, quatre tâches qui avancent en parallèle, une pull request prête à être relue." width="880"></a>

<sub>▶ <a href="https://getnohuman.com/">Voir la boucle</a> — un ticket en entrée, une pull request relue en sortie ; toute la boucle en 57 secondes.</sub>

</div>

> Ce document est une traduction du README anglais. En cas de divergence, la [version anglaise](README.md) fait foi. Les documents liés sont pour l'instant tous en anglais.

L'usine de code par IA à laquelle vous <ins>**pouvez faire confiance**</ins> :

- **Un plan avant la moindre ligne de code**, établi à partir du ticket et de ce
  qu'il trouve dans votre dépôt. Quand la planification échoue, on indique au
  codeur qu'il travaille sans plan ; quand le changement est jugé trivial, le
  plan est ignoré sans le dire au codeur, et c'est voulu — l'omission reste
  consignée dans le flux d'événements de l'exécution.
- **Une relecture contradictoire.** Un autre modèle, dans une session qui n'a jamais vu
  la transcription du codeur, chargé de réfuter le « terminé ». Vous obtenez une
  checklist réussite/échec citant fichier et ligne — jamais une auto-notation
  chiffrée.
- **Un garde-fou anti-altération.** Tests supprimés, nouveaux skips, assertion
  transformée en tautologie : le tout est compté mécaniquement avant que le
  contrôle de relecture ne s'exécute, puis justifié au regard de vos critères
  d'acceptation, faute de quoi la tentative s'arrête.
- **La preuve que le correctif corrige bien le bug.** Les tests présentés comme
  preuve doivent échouer sur la base de fusion et réussir sur le nouvel arbre —
  le contrôle de reproduction exécute les deux. Par défaut, cela s'applique aux
  corrections de bugs Python ; `repro_gate.mode: required` l'applique à tous les
  types de tâches et à tous les changements.
- **Vos tests sont exécutés**, en local et, si vous le voulez, via votre CI — et
  une PR pour laquelle aucune commande de test n'a été trouvée affiche **NOT RUN** noir sur blanc.
- **Un arrêt honnête.** Quand il ne peut pas finir, il s'arrête et dit pourquoi :
  une question précise si votre réponse le débloquerait, un compte rendu
  structuré s'il a simplement épuisé son budget — jamais un diff plausible
  inventé.

## Installation

### En une ligne (CLI + tableau)

```bash
uv tool install no-human   # ou : pipx install no-human — le wheel embarque le tableau
nh init && nh doctor       # token, config, premier dépôt ; puis vérifiez que l'installation tient debout
```

### Application de bureau

[![Télécharger pour macOS](https://img.shields.io/badge/T%C3%A9l%C3%A9charger%20pour-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Télécharger pour Windows](https://img.shields.io/badge/T%C3%A9l%C3%A9charger%20pour-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Télécharger pour Linux](https://img.shields.io/badge/T%C3%A9l%C3%A9charger%20pour-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

Chaque release livre un SHA-256 à côté de l'artefact. Notes par plateforme et
parcours du premier lancement : [docs/quickstart.md](docs/quickstart.md).

### Depuis les sources

```bash
git clone https://github.com/no-human-ai/no_human.git && cd no_human
uv sync                 # installe le point d'entrée `nh` dans .venv
(cd web && npm install && npm run build)   # construit le tableau (à froid, la première installation peut prendre plusieurs minutes)
uv run nh init          # token, config, premier dépôt (environ 2 minutes)
uv run nh doctor        # vérifiez que l'installation tient debout avant de compter dessus
```

Le build de `web` n'est pas facultatif si vous voulez le tableau : un clone des
sources ne contient aucun `web/dist`, donc sans lui `nh start` ne sert que
l'API et n'affiche aucune interface. Nécessite Python 3.12+,
[uv](https://github.com/astral-sh/uv), git, et Node avec npm pour le build du
tableau.

## Points forts du produit

<table>
  <tr>
    <td width="36%" valign="middle">
      <h3>Un plan avant la moindre ligne de code</h3>
      <p>Des critères d'acceptation que vous pouvez vérifier, écrits à partir du ticket et de votre dépôt.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-plan.png" alt="Le plan de la tâche : ce qui a été compris, sous forme de trois critères d'acceptation, les deux fichiers à modifier, l'approche, le plan de test, ce qui est hors périmètre, et la commande de vérification." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Un relecteur indépendant</h3>
      <p>Un second modèle qui n'a jamais vu la session du codeur, chargé de réfuter le « terminé ». Réussite ou échec ; chaque constat bloquant cite le fichier et la ligne.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-verdict.png" alt="Le verdict du relecteur : PASSED, chaque critère d'acceptation coché avec le fichier et la ligne qui le satisfont, une remarque non bloquante accompagnée du diff qu'elle vise." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Vos tests, en évidence sur la PR</h3>
      <p>Exécutés en local ou via votre CI. Si aucune commande de test n'est trouvée, on lit <b>NOT RUN</b>, jamais un champ vide.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tests.png" alt="Le panneau Test results de la tâche : CLEAN, 5 tests réussis sur 5, avec la sortie de pytest en dessous." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Un garde-fou anti-altération</h3>
      <p>Tests supprimés, nouveaux skips et assertions tautologiques sont comptés avant la relecture. Sans justification, la tentative s'arrête.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tamper.png" alt="Une tentative arrêtée : une bannière rouge TAMPER DETECTED, le verdict FAILED du relecteur, et le constat bloquant selon lequel trois tests ont été supprimés sans critère d'acceptation pour le justifier." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>La preuve que le correctif corrige bien le bug</h3>
      <p>Les tests présentés comme preuve doivent échouer sur l'ancien code et réussir sur le nouveau. Le contrôle exécute les deux, et le journal d'événements affiche le verdict.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-repro.png" alt="Le journal d'événements de la tâche : tests réussis, statut reviewing, le contrôle d'altération du relecteur à none, le contrôle de reproduction à pass, required, puis le lint, le commit et l'ouverture de la pull request." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Un arrêt honnête</h3>
      <p>Quand il a besoin de vous, il se met en attente avec une question précise au lieu de deviner.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-question.png" alt="La colonne Needs answer du tableau : une tâche en attente avec sa question, « Dedupe by user, or by digest id? », et un bouton Answer question ; à côté, les colonnes Working et Review PR." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Les tickets de votre outil de suivi, sur votre tableau</h3>
      <p>Choisissez des tickets Jira ou Linear dans le backlog (les tableaux monday.com sont récupérés par polling). Chacun est cadré avec vous avant de démarrer.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-backlog.png" alt="Le Backlog synchronisé depuis Jira : quatre tickets correspondants sélectionnés, et un bouton Start 4 tasks." width="100%" />
    </td>
  </tr>
</table>

<sub>Captures : le vrai tableau sur une charge de démonstration.</sub>

## Lancer une tâche

Lancez `nh` sans argument pour ouvrir le shell : vos colonnes, un flux
d'événements en direct, et un champ de saisie où vous décrivez une tâche en
langage courant. Toutes les commandes ci-dessous restent valables.

```bash
nh                                   # le shell
nh start                             # tableau + worker sur 127.0.0.1:8420
nh task add https://github.com/org/repo/issues/42 --repo ~/git/repo
nh status                            # needs-you / working / waiting / done
nh review <id>                       # la checklist de preuves du relecteur
nh diff <id>                         # le diff qu'il veut livrer
nh approve <id>                      # votre approbation fusionne la PR en squash (git.approve_identity)
nh reject <id> --reason "..."        # renvoie la tâche avec des remarques
```

## Intégrations

Pointez no_human vers l'outil de suivi que vous utilisez déjà et il fait
remonter les tickets sur votre tableau — le filtre d'un outil de suivi vit dans
votre configuration, jamais dans le texte d'une tâche, et une erreur de
transport est journalisée puis réessayée au tick suivant au lieu de faire tomber
le pool.

| Outil de suivi | Comment les tickets arrivent | Filtre à configurer |
|---|---|---|
| **Jira Cloud** | Polling de l'API REST `search/jql` (HTTP Basic `email:token`) | `integrations.jira.jql` |
| **Linear** | Polling de l'API GraphQL | `integrations.linear.team_key` + `state_types` + `label` |
| **monday.com** | Polling de l'API GraphQL v2 | `integrations.monday.board_id` + `status_column` + `todo_labels` |

Avec l'écriture en retour activée (`write_back`, désactivée par défaut), le
ticket suit la tâche — la correspondance se fait par catégorie de statut, par
type, ou par le label que vous indiquez, jamais par un id de transition codé en
dur — et reçoit le lien de la PR ; une tâche qui a besoin d'un humain reçoit un
commentaire, jamais une transition. Les issues GitHub et
GitLab s'importent comme tâches par URL, et les PR ou MR s'ouvrent sur votre
propre hôte ; Slack et Teams reçoivent un message quand une tâche a besoin de
vous ; Jenkins et CircleCI peuvent exécuter vos couches de tests et conditionner
la boucle. Configuration de chacun :
[docs/adapters.md](docs/adapters.md).

**Voyez le flux Jira de bout en bout** — des tickets synchronisés depuis un
tableau Jira, cadrés, implémentés, puis livrés sous forme de pull request ayant
passé la relecture (cliquez pour la vidéo complète, étape par étape) :

[![Démo du flux Jira](https://getnohuman.com/assets/demo-jira.gif)](https://getnohuman.com/assets/demo-jira.mp4)

<p align="center">▶️&nbsp;&nbsp;<strong><a href="https://getnohuman.com/assets/demo-jira.mp4">Lancer la démo complète</a></strong> — 1:33, du tableau Jira à une PR ayant passé la relecture</p>

## Serveur MCP — confiez-lui du travail depuis l'agent où vous êtes déjà

no_human embarque un **serveur MCP (Model Context Protocol)** : un pont stdio,
construit sur le SDK MCP Python officiel, qui permet à Claude Code, Cursor ou
n'importe quel client MCP de déposer du travail auprès de votre no_human local
et d'en suivre l'avancement.

```bash
nh mcp-serve        # le serveur MCP, via stdio
```

Deux outils, pas un de plus :

| Outil | Ce qu'il fait |
|---|---|
| `task_add(title, description, repo_path)` | Dépose une tâche. no_human la planifie ensuite, écrit le changement, exécute vos tests, la fait relire par un second modèle, et ouvre la pull request. |
| `task_status(task_id_or_external_id)` | Renvoie l'état courant de cette tâche — statut, tentatives, et le lien de la PR dès qu'il y en a un. |

Il ne parle qu'à votre propre no_human, sur `http://127.0.0.1:8420`, et à rien
d'autre : pas d'authentification, parce que cette adresse est localhost, et
aucun service à nous entre les deux. Pour Claude Code, le même serveur est
distribué comme plugin — ce dépôt est sa propre marketplace de plugins, si bien
que les deux outils apparaissent dans votre session après :

```
/plugin marketplace add no-human-ai/no_human
/plugin install no-human@no-human-ai
```

Tout autre client MCP se configure avec l'entrée stdio habituelle :

```jsonc
// .mcp.json
{ "mcpServers": { "no_human": { "command": "nh", "args": ["mcp-serve"] } } }
```

## Documentation

| | |
|---|---|
| [quickstart.md](docs/quickstart.md) | De zéro à la première tâche, par plateforme |
| [configuration.md](docs/configuration.md) | Chaque réglage et sa valeur par défaut |
| [verification.md](docs/verification.md) | Les contrôles, la boucle bornée, les limites |
| [security.md](docs/security.md) | Périmètre d'authentification, la règle « jamais de merge », les garde-fous |
| [blockers.md](docs/blockers.md) | Escalade, wake watcher, `nh reply` |
| [adapters.md](docs/adapters.md) | Ingestion, contexte, backends VCS et CI |
| [eval.md](docs/eval.md) | Jeu de référence, scoring par rejeu, mode shadow |
| [CHANGELOG.md](CHANGELOG.md) | Ce qui a changé, version par version |

## Développement

```bash
uv sync
uv run pytest -q
uv run nh --help
```

Les issues et les pull requests sont les bienvenues ; lancez `uv run pytest -q`
avant de soumettre.

Si no_human vous a évité un cycle de relecture, une étoile aide d'autres
personnes à le découvrir :
[![GitHub stars](https://img.shields.io/github/stars/no-human-ai/no_human?style=social)](https://github.com/no-human-ai/no_human/stargazers)

## Communauté

Questions, rapports de bugs et exécutions qui méritent d'être montrées :
rejoignez le [Discord](https://discord.gg/mSARvj6yW6), postez sur
[r/no_human](https://www.reddit.com/r/no_human/), ou ouvrez une
[GitHub issue](https://github.com/no-human-ai/no_human/issues).

## Licence

MIT — voir [LICENSE](LICENSE). La licence couvre le code, pas le nom :
[TRADEMARK.md](TRADEMARK.md) est la politique d'usage du nom « no_human » et du
logo. Empaqueter un binaire entraîne des obligations que l'arbre des sources n'a
pas ; elles sont listées dans
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
