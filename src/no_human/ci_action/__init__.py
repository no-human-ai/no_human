"""The GitHub Action entry point — one-shot, no daemon.

``ci_action`` is a SEPARATE product surface from ``nh review`` (which queues a
``code_review`` task for a running worker, ``cli/commands.py``). Everything
under this package runs as the single process a Docker-action step spawns,
does exactly one review, and exits. In particular it never:

* starts ``nh serve`` or any other long-lived process;
* opens or creates ``~/.no_human/no_human.db`` (or any other Store);
* imports ``core.orchestrator``, ``core.db``, ``core.store``, ``api``, or
  ``cli.commands`` — the queue, the worker loop and the CLI's own PR-watching
  machinery all live behind those modules, and pulling any of them in would
  reattach this Action to product state that must not exist inside a CI job.

The reviewer itself (``no_human.review.reviewer.AdversarialReviewer``) needs
none of that: it is constructed directly, handed an in-memory ``Task`` and a
diff, and returns a verdict. That direct call is the whole point of this
package existing — see ``run.py``.
"""

from __future__ import annotations
