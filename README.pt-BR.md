<div align="center">

<img src="docs/assets/nh-mark.png" alt="" width="140" height="140">

# no_human

<!-- mcp-name: io.github.no-human-ai/no_human -->

**Do ticket ao pull request revisado.**<br>***Gratuito e open source, na sua máquina.***

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · **Português (Brasil)**

[![latest release](https://img.shields.io/github/v/release/no-human-ai/no_human?label=release&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases/latest) [![CI](https://img.shields.io/github/actions/workflow/status/no-human-ai/no_human/ci.yml?branch=main&label=CI)](https://github.com/no-human-ai/no_human/actions/workflows/ci.yml) [![python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/) [![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![downloads](https://img.shields.io/github/downloads/no-human-ai/no_human/total?label=downloads&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases) [![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/mSARvj6yW6)

[getnohuman.com](https://getnohuman.com) · [Início rápido](docs/quickstart.md) · [Documentação](docs/README.md) · [Veja o no_human dar conta de uma sprint](https://getnohuman.com/demo) · [Discord](https://discord.gg/mSARvj6yW6)

[![Baixar para macOS](https://img.shields.io/badge/Baixar%20para-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Baixar para Windows](https://img.shields.io/badge/Baixar%20para-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Baixar para Linux](https://img.shields.io/badge/Baixar%20para-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

<a href="https://getnohuman.com/"><img src="docs/assets/hero-loop-poster.jpg" alt="O board do no_human: uma tarefa aguardando uma resposta na lane Needs answer, quatro tarefas rodando em paralelo e um pull request pronto para revisão." width="880"></a>

<sub>▶ <a href="https://getnohuman.com/">Veja o loop rodando</a> — entra um ticket, sai um pull request revisado; o loop inteiro em 57 segundos.</sub>

</div>

> Este documento é uma tradução do README em inglês. Em caso de divergência, a [versão em inglês](README.md) prevalece. Por enquanto, todos os documentos linkados estão em inglês.

A fábrica de código com IA em que você <ins>**pode confiar**</ins>:

- **Um plano antes de qualquer código**, feito a partir do ticket e do que ele
  encontra no seu repositório. Quando o planejamento falha, o coder é avisado de
  que está trabalhando sem plano; quando a mudança é julgada trivial, o plano é
  pulado sem avisar o coder, e isso é proposital — ainda assim, o plano pulado fica
  registrado no log de eventos da execução.
- **Uma revisão adversarial.** Um modelo diferente, numa sessão que nunca viu o
  transcript do coder, instruído a refutar o “pronto”. Você recebe um checklist
  de aprovado/reprovado citando arquivo e linha — nunca uma autoavaliação
  numérica.
- **Uma proteção anti-adulteração.** Testes apagados, novos skips, uma asserção
  transformada em tautologia — tudo contado mecanicamente antes de o gate de
  revisão rodar e depois confrontado com os seus critérios de aceite, ou a
  tentativa para.
- **Prova de que a correção corrigiu o bug.** Os testes apresentados como
  evidência precisam falhar no merge base e passar na árvore nova — o gate de
  reprodução roda os dois. De fábrica isso vale para uma correção de bug em
  Python; `repro_gate.mode: required` vale para todo tipo de tarefa e toda mudança.
- **Seus testes rodam**, localmente e, se você quiser, também pelo seu CI — e um
  PR em que nenhum comando de teste foi encontrado exibe **NOT RUN** logo de cara.
- **Uma parada honesta.** Quando não consegue terminar, ele para e diz por quê —
  uma pergunta específica quando a sua resposta destravaria a tarefa, um registro
  estruturado quando simplesmente acabou o orçamento — nunca um diff plausível
  inventado.

## Instalação

### Uma linha (CLI + board)

```bash
uv tool install no-human   # ou: pipx install no-human — o wheel já traz o board
nh init && nh doctor       # token, config, primeiro repo; depois prove que a instalação é real
```

### Aplicativo desktop

[![Baixar para macOS](https://img.shields.io/badge/Baixar%20para-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Baixar para Windows](https://img.shields.io/badge/Baixar%20para-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Baixar para Linux](https://img.shields.io/badge/Baixar%20para-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

Cada release traz um SHA-256 ao lado do artefato. Notas por plataforma e o passo
a passo da primeira execução: [docs/quickstart.md](docs/quickstart.md).

### A partir do código-fonte

```bash
git clone https://github.com/no-human-ai/no_human.git && cd no_human
uv sync                 # instala o entry point `nh` no .venv
(cd web && npm install && npm run build)   # compila o board (a primeira instalação, do zero, pode levar minutos)
uv run nh init          # token, config, primeiro repo (uns 2 minutos)
uv run nh doctor        # confirme que a instalação é real antes de depender dela
```

O build do `web` não é opcional se você quer o board: um checkout do código-fonte
não traz `web/dist`, então sem ele o `nh start` serve só a API e não renderiza
nenhuma UI. Precisa de Python 3.12+, [uv](https://github.com/astral-sh/uv), git e
Node com npm para o build do board.

## Destaques do produto

<table>
  <tr>
    <td width="36%" valign="middle">
      <h3>Um plano antes de qualquer código</h3>
      <p>Critérios de aceite que você consegue conferir, escritos a partir do ticket e do seu repositório.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-plan.png" alt="O plano da tarefa: o que entendemos, em três critérios de aceite, os dois arquivos a alterar, a abordagem, o plano de testes, o que ficou fora de escopo e o comando de verificação." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Um revisor independente</h3>
      <p>Um segundo modelo que nunca viu a sessão do coder, instruído a refutar o “pronto”. Aprova ou reprova; todo achado que bloqueia cita arquivo e linha.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-verdict.png" alt="O veredito do revisor: PASSED, cada critério de aceite marcado com o arquivo e a linha que o satisfazem, e uma observação não bloqueante com o diff a que ela se refere." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Seus testes, estampados no PR</h3>
      <p>Rodam localmente ou pelo seu CI. Se nenhum comando de teste for encontrado, aparece <b>NOT RUN</b>, nunca em branco.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tests.png" alt="O painel Test results da tarefa: CLEAN, 5 de 5 testes passaram, com a saída do pytest logo abaixo." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Uma proteção anti-adulteração</h3>
      <p>Testes apagados, novos skips e asserções tautológicas são contados antes da revisão. Sem justificativa, a tentativa para.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tamper.png" alt="Uma tentativa interrompida: um banner vermelho TAMPER DETECTED, o veredito FAILED do revisor e o achado bloqueante de que três testes foram apagados sem nenhum critério de aceite que justificasse a remoção." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Prova de que a correção corrigiu o bug</h3>
      <p>Os testes apresentados como evidência precisam falhar no código antigo e passar no novo. O gate roda os dois, e o log de eventos mostra o veredito.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-repro.png" alt="O log de eventos da tarefa: testes passam, status reviewing, a checagem de adulteração do revisor marcando none, o gate de reprodução marcando pass, required, depois lint, commit e a abertura do pull request." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Uma parada honesta</h3>
      <p>Quando precisa de você, ele deixa a tarefa em espera com uma pergunta específica em vez de chutar.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-question.png" alt="A lane Needs answer do board: uma tarefa em espera com a respectiva pergunta, “Dedupe by user, or by digest id?”, e um botão Answer question; ao lado, as lanes Working e Review PR." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Os tickets do seu tracker, no seu board</h3>
      <p>Escolha tickets do Jira ou do Linear direto do backlog (boards do monday.com são lidos por polling). Cada um tem o escopo fechado com você antes de começar.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-backlog.png" alt="O Backlog sincronizado do Jira: quatro tickets correspondentes selecionados e um botão Start 4 tasks." width="100%" />
    </td>
  </tr>
</table>

<sub>Imagens: o board real com uma carga de trabalho de demonstração.</sub>

## Rode uma tarefa

Rode `nh` sem argumentos para abrir o shell: suas lanes, um tail dos eventos ao
vivo e um campo onde você descreve uma tarefa em linguagem natural. Todos os
comandos abaixo continuam funcionando.

```bash
nh                                   # o shell
nh start                             # board + worker em 127.0.0.1:8420
nh task add https://github.com/org/repo/issues/42 --repo ~/git/repo
nh status                            # needs-you / working / waiting / done
nh review <id>                       # o checklist de evidências do revisor
nh diff <id>                         # o diff que ele quer entregar
nh approve <id>                      # sua aprovação faz o squash-merge do PR (git.approve_identity)
nh reject <id> --reason "..."        # devolve com feedback
```

## Integrações

Aponte o no_human para o tracker que você já usa e ele puxa os tickets para o seu
board — o filtro de um tracker fica na sua config, nunca no texto da própria
tarefa, e um erro de transporte é registrado no log e tentado de novo no próximo
tick, em vez de derrubar o pool.

| Tracker | Como os tickets chegam | Filtro que você configura |
|---|---|---|
| **Jira Cloud** | Polling via REST `search/jql` (HTTP Basic `email:token`) | `integrations.jira.jql` |
| **Linear** | Polling via API GraphQL | `integrations.linear.team_key` + `state_types` + `label` |
| **monday.com** | Polling via GraphQL v2 | `integrations.monday.board_id` + `status_column` + `todo_labels` |

Com o write-back ligado (`write_back`, desligado por padrão), o ticket anda junto
com a tarefa — casado por categoria de status, por tipo ou pela label que você
indicar, nunca por um id de transição hard-coded — e recebe o link do PR; uma
tarefa que precisa de um humano recebe um comentário, nunca uma transição. Issues
do GitHub e do GitLab entram como tarefas por URL, e PRs ou MRs são abertos no seu
próprio host; Slack e Teams recebem uma mensagem quando uma tarefa precisa de
você; Jenkins e CircleCI podem rodar suas camadas de teste e servir de gate para o
loop. A configuração de cada um:
[docs/adapters.md](docs/adapters.md).

**Veja o fluxo do Jira de ponta a ponta** — tickets sincronizados de um board do
Jira, com o escopo fechado, implementados e entregues como um pull request
aprovado na revisão (clique para o vídeo completo, com todos os passos):

[![Demo do fluxo com Jira](https://getnohuman.com/assets/demo-jira.gif)](https://getnohuman.com/assets/demo-jira.mp4)

<p align="center">▶️&nbsp;&nbsp;<strong><a href="https://getnohuman.com/assets/demo-jira.mp4">Assista à demo completa</a></strong> — 1:33, do board do Jira ao PR aprovado na revisão</p>

## Servidor MCP — passe trabalho a partir do agente em que você já está

O no_human vem com um **servidor MCP (Model Context Protocol)**: uma ponte stdio,
construída sobre o SDK oficial de MCP para Python, que permite ao Claude Code, ao
Cursor ou a qualquer cliente MCP registrar trabalho no seu no_human local e
acompanhar o andamento.

```bash
nh mcp-serve        # o servidor MCP, via stdio
```

Duas ferramentas, e nada além disso:

| Ferramenta | O que faz |
|---|---|
| `task_add(title, description, repo_path)` | Registra uma tarefa. O no_human então planeja, escreve a mudança, roda seus testes, põe um segundo modelo para revisar e abre o pull request. |
| `task_status(task_id_or_external_id)` | Retorna o estado atual daquela tarefa — status, tentativas e o link do PR assim que existir um. |

Ele fala com o seu próprio no_human em `http://127.0.0.1:8420` e com mais nada:
sem autenticação, porque esse endereço é localhost, e sem nenhum serviço nosso no
meio. Para o Claude Code, o mesmo servidor vem também como plugin — este
repositório é o próprio marketplace de plugins, então as duas ferramentas
aparecem na sua sessão depois de:

```
/plugin marketplace add no-human-ai/no_human
/plugin install no-human@no-human-ai
```

Qualquer outro cliente MCP usa a entrada stdio de sempre:

```jsonc
// .mcp.json
{ "mcpServers": { "no_human": { "command": "nh", "args": ["mcp-serve"] } } }
```

## Documentação

| | |
|---|---|
| [quickstart.md](docs/quickstart.md) | Do zero à primeira tarefa, por plataforma |
| [configuration.md](docs/configuration.md) | Cada opção de configuração e seu padrão |
| [verification.md](docs/verification.md) | Os gates, o loop limitado, os limites |
| [security.md](docs/security.md) | Fronteira de autenticação, a regra de nunca fazer merge, as proteções |
| [blockers.md](docs/blockers.md) | Escalonamento, wake watcher, `nh reply` |
| [adapters.md](docs/adapters.md) | Backends de intake, contexto, VCS e CI |
| [eval.md](docs/eval.md) | Golden set, pontuação por replay, modo shadow |
| [CHANGELOG.md](CHANGELOG.md) | O que mudou, a cada release |

## Desenvolvimento

```bash
uv sync
uv run pytest -q
uv run nh --help
```

Issues e pull requests são bem-vindos; rode `uv run pytest -q` antes de enviar.

Se o no_human te poupou um ciclo de revisão, uma estrela ajuda outras pessoas a
encontrá-lo:
[![GitHub stars](https://img.shields.io/github/stars/no-human-ai/no_human?style=social)](https://github.com/no-human-ai/no_human/stargazers)

## Comunidade

Dúvidas, relatos de bug e execuções que valem a pena mostrar: entre no [Discord](https://discord.gg/mSARvj6yW6),
poste no [r/no_human](https://www.reddit.com/r/no_human/) ou abra uma
[issue no GitHub](https://github.com/no-human-ai/no_human/issues).

## Licença

MIT — veja [LICENSE](LICENSE). A licença cobre o código, não o nome:
[TRADEMARK.md](TRADEMARK.md) é a política de uso de “no_human” e do logo.
Empacotar um binário traz obrigações que a árvore de código-fonte não tem,
listadas em [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
