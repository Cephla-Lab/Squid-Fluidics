"""The sequence list as data: the editor's model, with no Qt in it.

`SequenceList` holds the dicts, answers what a run would take
(`included`, `validated`), what is wrong with each row (`problem`,
`problems`, `blocking_error`), and performs the verbs that change it
(`replace`, `add`, `remove`, `duplicate`, `move`, `set_included`,
`set_all_included`, `set_name`, `set_field`). A GUI renders it and turns
clicks into these calls; a script, a headless embedder or a future API
can hold one without importing Qt at all.

Two rules the list owns rather than any view:

- **Coercion happens on the way out.** A row holds what was typed --
  never rewritten behind the operator -- and `problem` judges it by
  validating into a model rather than over the row itself; `validated`
  returns the coerced dicts a run or a save takes.
- **A name that equals the type's label is no name.** The row titles
  itself from the type when unnamed, so typing that title back (or
  emptying the field) must read as "unnamed" rather than freezing the
  label into the file.

The list does not announce its own changes: it is mutated by whoever
renders it, on that caller's thread, and every verb leaves it consistent
to read. A second reader would want a channel -- the shape the rest of
this package uses -- rather than polling.

There is no set_type verb. The editor retypes a row by removing and
adding, and a naive one would have to prune the old type's fields: the
models forbid extras, so a retyped row that kept them is invalid for
good.
"""

from .sequences import (SequenceListAdapter, is_included, sequence_problem,
                        type_label)


class SequenceList:
    """The rows, the verdicts, and the verbs that reorder them.

    application: the rig's application ("Flow Cell" / "Open Chamber"),
    which decides the sequence types on offer. port_limit: how many
    fluidic ports the rig has. Both are the config's, and both are read
    by every verdict -- set_config revalidates when a caller changes rigs
    under the same list.
    """

    def __init__(self, application, port_limit, sequences=()):
        self._application = application
        self._port_limit = port_limit
        self._rows = []
        self._problems = {}
        self.replace(sequences)

    def set_config(self, application, port_limit):
        """Point the list at a different rig, and re-judge it: a verdict
        outlives the config it was reached under otherwise."""
        self._application = application
        self._port_limit = port_limit
        self._revalidate()

    # --- reading ---

    def __len__(self):
        return len(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def __getitem__(self, row):
        return self._rows[row]

    is_included = staticmethod(is_included)     # what a run takes, one rule

    def included_rows(self):
        """Rows a run takes, in order."""
        return [row for row, seq in enumerate(self._rows)
                if self.is_included(seq)]

    def problem(self, row):
        """The verdict on one row, as a message, or None."""
        return self._problems.get(row)

    def problems(self):
        """{row: message} for every row that has one -- what an editor
        paints, and what a save has to refuse over."""
        return dict(self._problems)

    def blocking_error(self):
        """The first problem among the rows a run would actually take --
        an invalid row that is not checked blocks nothing."""
        for row in self.included_rows():
            if row in self._problems:
                return f"Sequence {row + 1}: {self._problems[row]}"
        return None

    def validated(self, included_only=False):
        """The list, validated and coerced -- the dicts a run or a save
        takes."""
        return self.included()[1] if included_only else self._coerced(
            range(len(self._rows)))

    def included(self):
        """(rows, sequences) for what a run would take: one read, so the
        rows a caller labels a plan with and the dicts it runs cannot come
        from two different moments."""
        rows = self.included_rows()
        return rows, self._coerced(rows)

    def _coerced(self, rows):
        validated = SequenceListAdapter.validate_python(
            [self._rows[row] for row in rows])
        return [seq.model_dump() for seq in validated]

    # --- writing ---

    def replace(self, sequences):
        """Take a new list, copied: the caller's dicts stay theirs."""
        self._rows = [dict(seq) for seq in sequences]
        self._revalidate()

    def add(self, seq):
        """Append `seq`; returns its row."""
        self._rows.append(dict(seq))
        self._revalidate()
        return len(self._rows) - 1

    def remove(self, row):
        """Drop `row`. Where the cursor goes next is the view's call."""
        self._rows.pop(row)
        self._revalidate()

    def duplicate(self, row):
        """Copy `row` in after itself; returns the copy's row. The copy is
        independent -- editing it must not edit the original."""
        self._rows.insert(row + 1, dict(self._rows[row]))
        self._revalidate()
        return row + 1

    def move(self, row, delta):
        """Swap `row` with its neighbour; returns where it landed, or None
        at the edges. The dicts themselves are swapped, so anything holding
        one by identity follows the move."""
        target = row + delta
        if not 0 <= target < len(self._rows):
            return None
        self._rows[row], self._rows[target] = self._rows[target], self._rows[row]
        self._revalidate()
        return target

    def set_included(self, row, included):
        self._rows[row]["include"] = bool(included)

    def set_all_included(self, included):
        for seq in self._rows:
            seq["include"] = bool(included)

    def title(self, row):
        """How the row names itself: its own name, else its type's label.
        The one place that rule is spelled -- a renderer and an edit both
        ask it rather than each composing the fallback."""
        seq = self._rows[row]
        return seq.get("name") or type_label(seq)

    def set_name(self, row, name):
        """Name the row, or unname it: blank, or the type's own label typed
        back, both mean unnamed."""
        seq = self._rows[row]
        name = (name or "").strip()
        seq["name"] = name if name and name != type_label(seq) else None
        self._validate_row(row)

    def set_field(self, row, field, raw):
        """Put a value in as given -- the editor's empty cell, and only
        that, reads as None. Coercion waits for `validated`; what is held
        is what was typed. Emptiness is the empty string, not falsiness:
        0 is a temperature and an incubation time, and a caller driving
        this model headlessly must be able to set one."""
        self._rows[row][field] = None if raw == "" else raw
        self._validate_row(row)

    # --- validation ---

    def _revalidate(self):
        self._problems = {}
        for row in range(len(self._rows)):
            self._validate_row(row)

    def _validate_row(self, row):
        problem = sequence_problem(self._rows[row], self._application,
                                   self._port_limit)
        if problem is None:
            self._problems.pop(row, None)
        else:
            self._problems[row] = problem
