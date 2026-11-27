"""Ground truth for a rename task without codenav: rope's rename (with the class hierarchy), as file:line edits.

    python rope_rename_truth.py PROJECT_DIR FILE LINE NAME NEW_NAME   (run with a Python that has rope installed)
Prints one `path:line: new line text` per edited line. Nothing is written to PROJECT_DIR."""

import difflib
import sys

from rope.base.project import Project
from rope.refactor.rename import Rename


root, rel, line, name, new = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
project = Project(root, ropefolder=None, ignored_resources=[".venv", "*.pyc", "__pycache__", ".git"])
resource = project.get_resource(rel)
text = resource.read()
offset = sum(len(row) + 1 for row in text.split("\n")[: line - 1]) + text.split("\n")[line - 1].index(name)
changes = Rename(project, resource, offset).get_changes(new, in_hierarchy=True, docs=False, unsure=lambda _: False)
for change in changes.changes:
	old = change.resource.read().split("\n")
	for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, old, change.new_contents.split("\n")).get_opcodes():
		if tag != "equal":
			for i in range(i1, i2):
				print(f"{change.resource.path}:{i + 1}: {old[i].strip()[:110]}")
