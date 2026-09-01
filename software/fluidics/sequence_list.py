"""The sequence list as data: what the operator typed, what a run would
take, and what is wrong with each row.

The editor's model, with no Qt in it. `SequenceList` holds the dicts --
exactly as typed, never rewritten behind the operator's back -- answers
what a run would take (`validated`, `included_rows`), what is wrong with
each row (`problem`, `blocking_error`), and performs the structural
verbs (add, remove, duplicate, move). A GUI renders it and translates
clicks into these calls; a script, a headless embedder or a future API
can hold one without importing Qt at all.

Two rules the list owns rather than any view:

- **Coercion happens on the way out.** `problem` validates a copy, so a
  half-typed field is a red row and not a rewritten one; `validated`
  returns the coerced dicts a run or a save takes.
- **A name that equals the type's label is no name.** The row titles
  itself from the type when unnamed, so typing that title back (or
  emptying the field) must read as "unnamed" rather than freezing the
  label into the file.
"""

from pydantic import ValidationError

from .sequences import (SEQUENCE_TYPE_LABELS, SequenceListAdapter,
                        sequence_port_problems, sequence_type_problem)


def type_label(seq):
    """How a row titles itself when it carries no name of its own."""
    seq_type = seq.get("type", "")
    return SEQUENCE_TYPE_LABELS.get(seq_type, seq_type)


class SequenceList:
    def __init__(self, application, port_limit, sequences=()):
        self.application = application
        self.port_limit = port_limit
        self._rows = []
        self._problems = {}
        self.replace(sequences)

    # --- reading ---

    def __len__(self):
        return len(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def __getitem__(self, row):
        return self._rows[row]

    @staticmethod
    def is_included(seq):
        """The include field, defaulting on -- the one spelling of what the
        checkbox means."""
        return seq.get("include", True)

    def included_rows(self):
        """Rows a run takes, in order."""
        return [row for row, seq in enumerate(self._rows)
                if self.is_included(seq)]

    def problem(self, row):
        """The verdict on one row, as a message, or None."""
        return self._problems.get(row)

    def blocking_error(self):
        """The first problem among the rows a run would actually take --
        an invalid row that is not checked blocks nothing."""
        for row in self.included_rows():
            if row in self._problems:
                return f"Sequence {row + 1}: {self._problems[row]}"
        return None

    def validated(self, included_only=False):
        """The list, validated and coerced -- the dicts a run or a save
        takes. `included_only` reads exactly `included_rows()`, so a
        snapshot of that list stays index-aligned with what is handed to
        the worker."""
        rows = self.included_rows() if included_only else range(len(self._rows))
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
        """Drop `row`; returns the row to select in its place."""
        self._rows.pop(row)
        self._revalidate()
        return min(row, len(self._rows) - 1)

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

    def set_name(self, row, name):
        """Name the row, or unname it: blank, or the type's own label typed
        back, both mean unnamed. Returns the title the row now shows."""
        seq = self._rows[row]
        name = (name or "").strip()
        seq["name"] = name if name and name != type_label(seq) else None
        self._validate_row(row)
        return seq["name"] or type_label(seq)

    def set_field(self, row, field, raw):
        """Put a typed value in as typed -- an empty field reads as None.
        Coercion waits for `validated`; what is held is what was typed."""
        self._rows[row][field] = raw if raw else None
        self._validate_row(row)

    # --- validation ---

    def _revalidate(self):
        self._problems = {}
        for row in range(len(self._rows)):
            self._validate_row(row)

    def _validate_row(self, row):
        problem = self._row_problem(self._rows[row])
        if problem is None:
            self._problems.pop(row, None)
        else:
            self._problems[row] = problem

    def _row_problem(self, seq):
        """A pure question: the row is never rewritten -- it holds what the
        operator typed; the coercion happens on a copy here, and for real
        in `validated`."""
        # The type first: a wrong-application row is also union-valid, and
        # for an unknown type this message beats the union's tag complaint.
        type_problem = sequence_type_problem(seq, self.application)
        if type_problem is not None:
            return type_problem
        try:
            validated = SequenceListAdapter.validate_python([seq])
        except ValidationError as e:
            first = e.errors()[0]
            field = ".".join(str(part) for part in first["loc"][2:]) or "sequence"
            return f"{field}: {first['msg']}"
        problems = sequence_port_problems(validated[0].model_dump(),
                                          self.port_limit)
        if problems:
            return ("; ".join(problems)
                    + f": this configuration has ports 1..{self.port_limit}")
        return None
