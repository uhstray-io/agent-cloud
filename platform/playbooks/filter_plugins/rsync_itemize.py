"""Did an `rsync -i` run change the destination? (tasks/place-monorepo.yml, local mode.)

`changed_when: stdout | length > 0` calls a run changed whenever rsync printed anything, and
`-i` prints a line for attribute-only differences that no deploy can see. Measured on rsync
3.5.0 with the checkout bind-mounted read-only (as the local Semaphore does): every run after
the first printed `.L...p..... deployment/AGENTS.md -> CLAUDE.md` for each tracked symlink,
because the mount reports a symlink mode the destination cannot be set to (Linux has no
lchmod), and `.d..t...... dir/` whenever a directory mtime differed. Both would make a second,
identical deploy report "changed".

The format is `YXcstpoguax` (rsync(1), --itemize-changes): Y is the update type, X the file
type, then one position per attribute. Counted as a change:

  * `*deleting`   a path removed by --delete
  * Y in `<>c`    a transfer, or a created directory or symlink, or a symlink whose target
                  changed (`cLc.t......`). A file whose bytes changed at the same size shows
                  only `>f..t......`, so any transfer counts; the bytes cannot be told apart
                  from a touch without --checksum, and a touch is a real source difference.
  * Y is `h`      a hard link
  * `.f` `.d` `.D` `.S` with `p`   a mode change on a file, directory or special file. A
                  deploy.sh losing its exec bit changes what a deploy does.

Not counted: `.L` (a symlink whose only difference is its mode or time: nothing reads it),
`.d` with only `t` (also dropped at the source by --omit-dir-times), and any line that is not
an itemize line (rsync's "skipping non-regular file" notes).
"""

import re

_CHANGE = re.compile(
    r"""^(?:
        \*deleting
      | [<>ch][fdLDS]\S{9}[ ]
      | \.[fdDS]\S{3}p\S{5}[ ]
    )""",
    re.VERBOSE,
)


def rsync_itemize_changed(stdout):
    """True when any line of `rsync -i` stdout records a real change to the destination."""
    return any(_CHANGE.match(line) for line in str(stdout).splitlines())


class FilterModule:
    def filters(self):
        return {"rsync_itemize_changed": rsync_itemize_changed}
