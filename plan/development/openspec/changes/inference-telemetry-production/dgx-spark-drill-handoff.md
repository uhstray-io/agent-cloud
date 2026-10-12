# dgx-spark request: access for the node-exporter fault drill (task 2.5)

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Prepared 2026-10-12.

**Status: PREPARED, NOT SENT.** Operator decision 2026-10-12: write the request, deliver
it to nobody (no issue, no pull request and no message in the dgx-spark repository).
Task 2.5 in `tasks.md` stays open and blocked on this request until the operator sends it.

Boundary (proposal.md): the two DGX Spark machines and the vLLM API are dgx-spark's.
Nothing here is applied from agent-cloud. Everything under "What agent-cloud asks of
dgx-spark" is a change dgx-spark makes on its own nodes, in its own repository, in its
own window. No IP address, credential or key value appears in this file; the node
addresses live in `site-config` and the key lives in OpenBao.

## Why this is needed

Task 2.5 proves the scenario "Missing scrape is a telemetry failure": stop one node
exporter, wait past the alert's pending window, see the target at `up == 0` and the
`telemetry-missing` rule firing, then restore the exporter. The playbook is
`platform/playbooks/o11y-fault-drill.yml` with `-e drill=exporter`.

Two facts block it today:

1. **The nodes are not inventory hosts.** The drill refuses unless the declared exporter
   host is in the inventory (`platform/playbooks/o11y-fault-drill.yml:179`,
   `_exporter.host | default('') in groups['all']`). `site-config`
   `inventory/production.yml` has no DGX hosts. It carries only the scrape list
   `dgx_spark_nodes` (name and address for `spark-1` and `spark-2`, `:345-347` on
   `origin/main`) under the o11y service group's variables, which makes the nodes
   scrape targets, not hosts Ansible can reach.
2. **agent-cloud has no login on the nodes.** The drill stops and starts the unit on the
   exporter host with `delegate_to: "{{ _exporter.host }}"`
   (`o11y-fault-drill.yml:282-286` stop, `:457-461` restore in the `always:` block).
   dgx-spark's own inventory authenticates with local secret files that agent-cloud must
   not read or copy (OpenBao is the credential source here).

## What agent-cloud asks of dgx-spark

For `spark-1` and `spark-2` (the drill stops one node at a time, so both need the same
access, but only the node named by `-e drill_node=` is ever touched):

1. **A dedicated unprivileged account** with no general sudo, and the agent-cloud
   per-service SSH public key in its `authorized_keys`. agent-cloud generates the keypair
   and holds the private half in OpenBao; dgx-spark receives only the public half
   through whichever channel it chooses (agent-cloud will not push it to the nodes while
   they are outside its inventory).
2. **A polkit rule that lets that account, and only that account, start and stop
   `node-exporter.service`.** polkit is the path, not sudo (see "Why polkit and not
   sudo"). Nothing else is requested: no other unit, no `daemon-reload`, no unit-file
   management, no shell as root.
   - The systemd action is `org.freedesktop.systemd1.manage-units` (defined in systemd
     `src/core/org.freedesktop.systemd1.policy.in`, default `auth_admin`).
   - PID 1 adds the details `unit` and `verb` to the polkit request only when the call
     has them (systemd `src/core/dbus-util.c`, `bus_verify_manage_units_async_impl`, the
     `if (id)` and `if (verb)` branches).
   - **The rule MUST require `action.lookup("unit") === "node-exporter.service"`. An
     absent unit is not a match.** SHOULD also require `action.lookup("verb")` to be
     `"start"` or `"stop"`.
   - **A rule on the action id and the account alone is out of bounds: it is
     root-equivalent.** The same action also authorizes the manager-level calls, which
     carry no `unit` detail (`bus_verify_manage_units_async` passes none; used by
     `method_enqueue_unit_job_many`, `method_clear_jobs`, `method_reset_failed` and
     `method_enqueue_marked_jobs` in systemd `src/core/dbus-manager.c:893`, `:1309`,
     `:1330`, `:2223`), and `StartTransientUnit` (`method_start_transient_unit`), which
     does carry a `unit` (the new unit's own name, `dbus-manager.c:1226-1232`) and
     creates a unit running a command of the caller's choosing, which is what
     `systemd-run` as root rests on. The unit match denies the manager-level calls
     because they carry no unit, and denies a transient unit because its name is not
     `node-exporter.service`. All line numbers are systemd `main` read on 2026-10-12,
     not the nodes' installed version.
   - Shape of the rule (an example; dgx-spark writes, places and tests the real one):

     ```js
     polkit.addRule(function(action, subject) {
         if (action.id === "org.freedesktop.systemd1.manage-units" &&
             subject.user === "<dedicated account>" &&
             action.lookup("unit") === "node-exporter.service" &&
             (action.lookup("verb") === "start" || action.lookup("verb") === "stop")) {
             return polkit.Result.YES;
         }
     });
     ```

     The rule returns nothing for any other request, so the action's own default
     (`auth_admin`) applies to it; dgx-spark confirms that behaviour on a node.
   - unverified, dgx-spark to confirm on each node: that polkit is installed and its
     JavaScript `.rules` files are supported (a version of 0.106 or later is the
     expectation, not confirmed); the exact `verb` strings the installed systemd sends for
     stop and start; and that a call for any other unit, and a manager-level call, are
     refused for the account after the rule is placed.
3. **Firewall:** allow SSH (the port is dgx-spark's to name) from the Semaphore runner
   host to both nodes. The runner's address is in `site-config`, not here. The existing
   scrape path from the o11y VM to the exporter port is unaffected and unchanged.
4. **Confirm the unit and agree a window.**
   - The unit is `node-exporter.service`: managed by dgx-spark `telemetry-host-exporter.yml`
     (`/etc/systemd/system/node-exporter.service`, boot-enabled) and described in
     `docs/HARDWARE.md` ("Host exporter serving pilot (2026-09-22)"), read on `origin/main`
     at commit `d803163`.
   - Confirm that stopping it on one node for the drill's duration (twelve minutes of
     wait, plus scrape and evaluation delay, per task 2.5) is safe: that nothing on the
     node (the vLLM ranks, a benchmark sweep) depends on the exporter, and that no
     dgx-spark automation (a `telemetry.yml` run, for example) is scheduled in that
     window and would fight the stop. agent-cloud has not verified either point.
   - Name the window. The drill refuses without `-e drill_window_confirmed=true`.
   - The drill restores in an `always:` block; if the runner connection is lost, an
     operator re-runs with `drill_restore_only=true`. dgx-spark may also want its own
     check that the unit is `active` afterwards.

### Why polkit and not sudo

agent-cloud first planned a sudoers rule limited to `systemctl stop|start|is-active
node-exporter.service`. That cannot work for this drill as written: the drill uses
`ansible.builtin.systemd_service` with `become: true`, and Ansible's sudo become does not
run `systemctl` directly. The become plugin in ansible-core (read at 2.20.8, the
production runner's version; `ansible/plugins/become/__init__.py:67-77`,
`_build_success_command`, unchanged in the workstation's 14.0.0 package) wraps the module
command as `<shell> -c 'echo BECOME-SUCCESS-<id> ; <module command>'`, so a sudoers
entry naming only `systemctl` rejects every task. Granting broader sudo to make it pass
would defeat the least-privilege goal, so the request asks for polkit and agent-cloud
changes the drill to run unprivileged (follow-up 1 below).

## What agent-cloud does afterwards (NOT done, listed for the record)

None of this is started, and none of it touches a DGX node.

1. `platform/playbooks/o11y-fault-drill.yml`: set `become: false` on the two exporter
   tasks (`:282-286` stop, `:457-461` restore); keep `become: true` on the probe tasks.
   Extend `platform/tests/test_o11y_fault_drill.py`. The change lands only after dgx-spark
   confirms the polkit rule; until then `become: true` on those tasks is what the
   playbook does.
2. Generate the per-service key with `generate-service-ssh-key.yml` and back it up to
   `site-config` (`backup-service-ssh-key.yml`).
3. `site-config` `inventory/production.yml`: add `spark-1` and `spark-2` as hosts in a
   dedicated group, with the dedicated account as `ansible_user` and the per-service
   key. Check first that no play targeting `all` would pick them up unintentionally.
4. Once dgx-spark reports the public half installed (installing it is dgx-spark's step,
   so `distribute-ssh-keys.yml` is not used on these nodes), verify key-only login with
   `verify-host-access.yml`, scoped to the new group.
5. Declare the map in `site-config`:
   `o11y_fault_drill_exporters: {spark-1: {host: spark-1, unit: node-exporter.service},
   spark-2: {host: spark-2, unit: node-exporter.service}}` (shape per
   `o11y-fault-drill.yml:37-39`).
6. Dry run: `o11y-fault-drill.yml -e drill=exporter` under `--check` from Semaphore
   (every refusal and read runs, the fault block is skipped).
   unverified: that `systemd_service` runs cleanly as an unprivileged user under polkit
   on the nodes; the first real run in the agreed window proves it, and the restore step
   is idempotent if it does not.
7. One real run in the agreed window, then record the evidence on task 2.5 and tick it,
   which also closes the missing half of the 2.6 validation gate.

## Out of scope

- The GPU exporter (deliberately not scraped), vLLM, Ray, and any change to the nodes'
  telemetry configuration.
- Sending this request. Per the operator it is delivered only on their say-so.
