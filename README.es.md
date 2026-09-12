<div align="center">

<img src="docs/assets/nh-mark.png" alt="" width="140" height="140">

# no_human

<!-- mcp-name: io.github.no-human-ai/no_human -->

**Del ticket a un pull request revisado.**<br>***Gratis y de código abierto, en tu máquina.***

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · **Español** · [Français](README.fr.md) · [Deutsch](README.de.md) · [Português (Brasil)](README.pt-BR.md)

[![latest release](https://img.shields.io/github/v/release/no-human-ai/no_human?label=release&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases/latest) [![CI](https://img.shields.io/github/actions/workflow/status/no-human-ai/no_human/ci.yml?branch=main&label=CI)](https://github.com/no-human-ai/no_human/actions/workflows/ci.yml) [![python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/) [![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![downloads](https://img.shields.io/github/downloads/no-human-ai/no_human/total?label=downloads&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases) [![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/mSARvj6yW6)

[getnohuman.com](https://getnohuman.com) · [Inicio rápido](docs/quickstart.md) · [Documentación](docs/README.md) · [Míralo trabajar un sprint](https://getnohuman.com/demo) · [Discord](https://discord.gg/mSARvj6yW6)

[![Descargar para macOS](https://img.shields.io/badge/Descargar%20para-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Descargar para Windows](https://img.shields.io/badge/Descargar%20para-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Descargar para Linux](https://img.shields.io/badge/Descargar%20para-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

<a href="https://getnohuman.com/"><img src="docs/assets/hero-loop-poster.jpg" alt="El tablero de no_human: una tarea esperando una respuesta en Needs answer, cuatro tareas trabajando en paralelo y un pull request listo para revisar." width="880"></a>

<sub>▶ <a href="https://getnohuman.com/">Mira el bucle</a> — entra un ticket, sale un pull request revisado; el bucle entero en 57 segundos.</sub>

</div>

> Este documento es una traducción del README en inglés. En caso de discrepancia, prevalece la [versión en inglés](README.md). Por ahora, todos los documentos enlazados están en inglés.

La fábrica de código con IA en la que <ins>**puedes confiar**</ins>:

- **Un plan antes de escribir código**, a partir del ticket y de lo que
  encuentra en tu repositorio. Cuando la planificación falla, al coder se le
  dice que está trabajando sin plan; cuando el cambio se juzga trivial, el plan
  se omite sin avisar al coder, y es deliberado — la omisión sí se hace constar
  en el flujo de eventos de la ejecución.
- **Una revisión adversarial.** Otro modelo, en una sesión que nunca vio la
  transcripción del coder, con la instrucción de refutar el «terminado».
  Obtienes una lista de verificación de aprobado/rechazado que cita archivo y
  línea — nunca una autopuntuación numérica.
- **Una protección antimanipulación.** Tests borrados, skips nuevos, una
  aserción convertida en tautología: se cuentan mecánicamente antes de que se
  ejecute la puerta de revisión y luego se justifican frente a tus criterios de
  aceptación, o el intento se detiene.
- **Prueba de que el arreglo arregló el bug.** Los tests que se ofrecen como
  evidencia tienen que fallar en la base de merge y pasar en el árbol nuevo — la
  puerta de reproducción ejecuta ambas cosas. De fábrica, eso se aplica a un
  arreglo de bug en Python; `repro_gate.mode: required` lo aplica a cualquier tipo
  de cambio.
- **Tus tests se ejecutan**, en local y, si quieres, a través de tu CI — y un PR
  en el que no se encontró ningún comando de test lo indica bien visible:
  **NOT RUN**.
- **Una parada honesta.** Cuando no puede terminar, se detiene y dice por qué:
  una pregunta concreta cuando tu respuesta lo desbloquearía, un registro
  estructurado cuando simplemente se quedó sin presupuesto — nunca un diff
  plausible inventado.

## Instalación

### En una línea (CLI + tablero)

```bash
uv tool install no-human   # o: pipx install no-human — el wheel incluye el tablero
nh init && nh doctor       # token, configuración, primer repo; luego comprueba que la instalación es real
```

### Aplicación de escritorio

[![Descargar para macOS](https://img.shields.io/badge/Descargar%20para-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Descargar para Windows](https://img.shields.io/badge/Descargar%20para-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Descargar para Linux](https://img.shields.io/badge/Descargar%20para-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

Cada release publica un SHA-256 junto al artefacto. Notas por plataforma y el
recorrido del primer arranque: [docs/quickstart.md](docs/quickstart.md).

### Desde el código fuente

```bash
git clone https://github.com/no-human-ai/no_human.git && cd no_human
uv sync                 # instala el entry point `nh` en .venv
(cd web && npm install && npm run build)   # compila el tablero (la primera instalación en frío puede tardar minutos)
uv run nh init          # token, configuración, primer repo (unos 2 minutos)
uv run nh doctor        # comprueba que la instalación es real antes de depender de ella
```

Compilar `web` no es opcional si quieres el tablero: una copia del código fuente
no trae `web/dist`, así que sin eso `nh start` sirve solo la API y no muestra
ninguna UI. Necesita Python 3.12+, [uv](https://github.com/astral-sh/uv), git y
Node con npm para compilar el tablero.

## Lo que destaca del producto

<table>
  <tr>
    <td width="36%" valign="middle">
      <h3>Un plan antes de escribir código</h3>
      <p>Criterios de aceptación que puedes comprobar, escritos a partir del ticket y de tu repositorio.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-plan.png" alt="El plan de la tarea: lo que entendimos, en tres criterios de aceptación; los dos archivos que hay que cambiar; el enfoque; el plan de tests; lo que queda fuera de alcance; y el comando de verificación." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Un revisor independiente</h3>
      <p>Un segundo modelo que nunca vio la sesión del coder, con la instrucción de refutar el «terminado». Aprobado o rechazado; cada hallazgo que bloquea cita archivo y línea.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-verdict.png" alt="El veredicto del revisor: PASSED, cada criterio de aceptación marcado con el archivo y la línea que lo satisfacen, y un detalle menor no bloqueante con el diff al que apunta." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Tus tests, a la vista en el PR</h3>
      <p>Se ejecutan en local o a través de tu CI. Si no se encuentra ningún comando de test, aparece <b>NOT RUN</b>; nunca se queda en blanco.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tests.png" alt="El panel Test results de la tarea: CLEAN, 5 superados de 5, con la salida de pytest debajo." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Una protección antimanipulación</h3>
      <p>Los tests borrados, los skips nuevos y las aserciones tautológicas se cuentan antes de la revisión. Sin justificación, el intento se detiene.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tamper.png" alt="Un intento detenido: un banner rojo de TAMPER DETECTED, el veredicto del revisor en FAILED y el hallazgo bloqueante de que se borraron tres tests sin ningún criterio de aceptación que lo justifique." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Prueba de que el arreglo arregló el bug</h3>
      <p>Los tests que se ofrecen como evidencia tienen que fallar con el código viejo y pasar con el nuevo. La puerta ejecuta ambas cosas, y el registro de eventos muestra el veredicto.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-repro.png" alt="El registro de eventos de la tarea: los tests pasan, el estado es reviewing, la comprobación de manipulación del revisor marca none, la puerta de reproducción marca pass, required, y después lint, commit y la apertura del pull request." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Una parada honesta</h3>
      <p>Cuando te necesita, queda en espera con una pregunta concreta en lugar de adivinar.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-question.png" alt="El carril Needs answer del tablero: una tarea en espera con su pregunta, 'Dedupe by user, or by digest id?', y un botón Answer question; al lado, los carriles Working y Review PR." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>Los tickets de tu tracker, en tu tablero</h3>
      <p>Elige tickets de Jira o de Linear desde el backlog (los tableros de monday.com se consultan por polling). El alcance de cada uno se acuerda contigo antes de empezar.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-backlog.png" alt="El Backlog sincronizado desde Jira: cuatro tickets coincidentes seleccionados y un botón Start 4 tasks." width="100%" />
    </td>
  </tr>
</table>

<sub>Capturas: el tablero real con una carga de trabajo de demostración.</sub>

## Ejecuta una tarea

Ejecuta `nh` sin argumentos para abrir la shell: tus carriles, un seguimiento de
eventos en vivo y un campo de entrada donde describes una tarea en lenguaje
natural. Todos los comandos de abajo siguen funcionando.

```bash
nh                                   # la shell
nh start                             # tablero + worker en 127.0.0.1:8420
nh task add https://github.com/org/repo/issues/42 --repo ~/git/repo
nh status                            # needs-you / working / waiting / done
nh review <id>                       # la lista de verificación con evidencias del revisor
nh diff <id>                         # el diff que quiere entregar
nh approve <id>                      # tu aprobación hace squash-merge del PR (git.approve_identity)
nh reject <id> --reason "..."        # devuélvelo con feedback
```

## Integraciones

Apunta no_human al tracker que ya usas y traerá los tickets a tu tablero: el
filtro de un tracker vive en tu configuración, nunca en el texto de una tarea, y
un error de transporte se registra y se reintenta en el siguiente tick en lugar
de tumbar el pool.

| Tracker | Cómo llegan los tickets | Filtro que configuras |
|---|---|---|
| **Jira Cloud** | Polling vía REST `search/jql` (HTTP Basic `email:token`) | `integrations.jira.jql` |
| **Linear** | Polling vía la API GraphQL | `integrations.linear.team_key` + `state_types` + `label` |
| **monday.com** | Polling vía la API GraphQL v2 | `integrations.monday.board_id` + `status_column` + `todo_labels` |

Con la escritura de vuelta activada (`write_back`, desactivada por defecto), el
ticket se mueve junto con la tarea — emparejado por categoría de estado, por
tipo o por la etiqueta que indiques, nunca por un id de transición fijado en el
código — y recibe el enlace al PR; una tarea que necesita a una persona recibe
un comentario, nunca una transición. Las issues de GitHub y
GitLab se importan como tareas por URL, y los PR o MR se abren en tu propio
host; Slack y Teams reciben un mensaje cuando una tarea te necesita; Jenkins y
CircleCI pueden ejecutar tus capas de tests y actuar como puerta del bucle. La
configuración de cada uno:
[docs/adapters.md](docs/adapters.md).

**Mira el flujo de Jira de principio a fin** — tickets sincronizados desde un
tablero de Jira, acotados, implementados y entregados como un pull request que
pasó la revisión (haz clic para ver el video completo con todos los pasos):

[![Demo del flujo de Jira](https://getnohuman.com/assets/demo-jira.gif)](https://getnohuman.com/assets/demo-jira.mp4)

<p align="center">▶️&nbsp;&nbsp;<strong><a href="https://getnohuman.com/assets/demo-jira.mp4">Reproduce la demo completa</a></strong> — 1:33, del tablero de Jira al PR que pasó la revisión</p>

## Servidor MCP — pásale trabajo desde el agente en el que ya estás

no_human incluye un **servidor MCP (Model Context Protocol)**: un puente stdio,
construido sobre el SDK oficial de MCP para Python, que permite a Claude Code, a
Cursor o a cualquier cliente MCP encargarle trabajo a tu no_human local y
consultar cómo va.

```bash
nh mcp-serve        # el servidor MCP, sobre stdio
```

Dos herramientas, y ninguna más:

| Herramienta | Qué hace |
|---|---|
| `task_add(title, description, repo_path)` | Registra una tarea. no_human la planifica, escribe el cambio, ejecuta tus tests, hace que un segundo modelo la revise y abre el pull request. |
| `task_status(task_id_or_external_id)` | Devuelve en qué punto está esa tarea: estado, intentos y el enlace al PR en cuanto existe. |

Habla con tu propio no_human en `http://127.0.0.1:8420` y con nada más: sin
autenticación, porque esa dirección es localhost, y sin ningún servicio nuestro
por medio. Para Claude Code, el mismo servidor se distribuye como plugin — este
repositorio es su propio marketplace de plugins, así que las dos herramientas
aparecen en tu sesión después de:

```
/plugin marketplace add no-human-ai/no_human
/plugin install no-human@no-human-ai
```

Cualquier otro cliente MCP admite la entrada stdio de siempre:

```jsonc
// .mcp.json
{ "mcpServers": { "no_human": { "command": "nh", "args": ["mcp-serve"] } } }
```

## Documentación

| | |
|---|---|
| [quickstart.md](docs/quickstart.md) | De cero a la primera tarea, plataforma por plataforma |
| [configuration.md](docs/configuration.md) | Todos los ajustes y sus valores por defecto |
| [verification.md](docs/verification.md) | Las puertas, el bucle acotado, los límites |
| [security.md](docs/security.md) | La frontera de autenticación, la regla de nunca hacer merge, las protecciones |
| [blockers.md](docs/blockers.md) | Escalado, wake watcher, `nh reply` |
| [adapters.md](docs/adapters.md) | Entrada de tickets, contexto, backends de VCS y de CI |
| [eval.md](docs/eval.md) | Golden set, puntuación de replays, modo shadow |
| [CHANGELOG.md](CHANGELOG.md) | Qué cambió en cada release |

## Desarrollo

```bash
uv sync
uv run pytest -q
uv run nh --help
```

Las issues y los pull requests son bienvenidos; ejecuta `uv run pytest -q` antes
de enviar.

Si no_human te ahorró un ciclo de revisión, una estrella ayuda a que otras
personas lo encuentren:
[![GitHub stars](https://img.shields.io/github/stars/no-human-ai/no_human?style=social)](https://github.com/no-human-ai/no_human/stargazers)

## Comunidad

Preguntas, informes de errores y ejecuciones que merezca la pena enseñar: únete
al [Discord](https://discord.gg/mSARvj6yW6), publica en
[r/no_human](https://www.reddit.com/r/no_human/) o abre un
[issue en GitHub](https://github.com/no-human-ai/no_human/issues).

## Licencia

MIT — consulta [LICENSE](LICENSE). La licencia cubre el código, no el nombre:
[TRADEMARK.md](TRADEMARK.md) es la política de uso de «no_human» y del logo.
Empaquetar un binario conlleva obligaciones que el árbol de fuentes no tiene,
listadas en [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
