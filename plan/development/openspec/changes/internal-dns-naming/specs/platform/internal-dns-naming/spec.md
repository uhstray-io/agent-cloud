# platform/internal-dns-naming

How internal names are built, declared, rendered, validated and reconciled with IPAM, so
that capacity scales out and roles move without any client or certificate changing the
name it uses. `<zone>` and `<site>` are declared in site-config.

## ADDED Requirements

### Requirement: Internal names follow four families under a site label
The platform SHALL build every internal name in one of four families under
`<site>.<zone>`: service `<service>.<site>.<zone>`, instance
`<class><NN>.<service>.<site>.<zone>`, host `<hostname>.host.<site>.<zone>` and management
`<hostname>.mgmt.<site>.<zone>`. A management name MUST exist only for a management
address distinct from the host address. Clients MUST address a service by its service
name.
Instance ordinals MUST be two digits and a retired ordinal MUST NOT be declared again.

#### Scenario: Scale-out adds an instance without a client change
- WHEN a second serving member is declared for a service that has no front, and the DNS
  deploy runs
- THEN the service name answers with both members' addresses, the new instance name
  exists, and no client configuration or certificate that uses the service name changes;
  a load balancer in front of the pool changes only if it lists members by instance name
  (the gateway does until its handling of several addresses is proven)

#### Scenario: A retired ordinal is refused
- WHEN a host declares an instance whose service and ordinal are listed as retired
- THEN the render fails naming that instance and no zone file is written

### Requirement: A service name resolves to its front door
A service declared with a front SHALL render as a CNAME to the front's service name. A
service without a front SHALL render one A record per serving member, taken from that
member's host record. A member declared as not serving MUST keep its instance name and
MUST NOT appear in the service's A records or in any load-balancer backend list.

#### Scenario: A load-balanced service points at the load balancer
- WHEN `inference` is declared with front `gateway`
- THEN `inference.<site>.<zone>` is a CNAME to `gateway.<site>.<zone>` and carries no A
  record of its own

#### Scenario: A non-serving member is named but not pooled
- WHEN the Ray worker is declared as instance `dgx02` of `vllm-primary` with
  `serves: false`
- THEN `dgx02.vllm-primary.<site>.<zone>` resolves to its host name and the answer for
  `vllm-primary.<site>.<zone>` does not contain its address

#### Scenario: Moving the front door is one record
- WHEN the front of `inference` changes from one service to another
- THEN the rendered zone differs from the previous one in the `inference` record only

### Requirement: An instance name is an alias of its host name
Each instance name SHALL be a CNAME to its member's host name, and each host's address
SHALL appear in exactly one host record. Moving an instance to another host MUST change
only that instance's CNAME and, for a serving member of a service without a front, that
service's A records.

#### Scenario: A role move changes one CNAME
- WHEN the declaration of a non-serving instance moves from one host to another and the
  DNS deploy runs
- THEN the only record that differs in the rendered zone is that instance's CNAME, which
  now names the new host

### Requirement: Labels are validated and reserved labels are refused at render
The DNS deploy SHALL validate every declared name before it writes any file. Each label
MUST match `^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$`, except SRV owner labels, which start with
`_`, and each full name MUST be at most 253 characters. A service name MUST NOT be `host`,
`mgmt`, `ns`, a label starting with `_`, or any declared site label. The render MUST
refuse a name outside `<site>.<zone>` other than the SOA, NS and apex records, a name
declared twice, a CNAME owner that carries any other record, and, when not in local mode,
a wildcard record.

#### Scenario: A reserved label is refused at render
- WHEN a service named `host` is declared and the DNS deploy runs
- THEN the render fails naming the reserved label, and the zone the server was answering
  from before the run is unchanged

#### Scenario: An invalid label is refused
- WHEN a declared name contains an upper-case letter, an underscore outside an SRV owner
  label, a label longer than 63 characters, or a label that starts with a hyphen or digit
- THEN the render fails naming the offending label and no zone file is written

#### Scenario: A production wildcard is refused
- WHEN a wildcard record is declared for an inventory that is not in local mode
- THEN the render fails, and a query for an undeclared name under `<site>.<zone>` answers
  NXDOMAIN

### Requirement: Machines this repository does not manage are declared as records only
Machines that no playbook in this repository manages SHALL be declared as entries in the
`dns_records_only_hosts` variables list, never as inventory hosts or groups, so that no
play's host pattern can match them. The render MUST produce the same host, instance and
reverse records for a list entry as for a managed host, and MUST refuse a hostname that
is both a list entry and an inventory host.

#### Scenario: Records-only machines are never inventory hosts
- WHEN the DGX Spark nodes are declared in `dns_records_only_hosts` and the DNS deploy
  renders the zone
- THEN their host and instance records are rendered, a play whose host pattern is `all`
  matches none of them, and a render in which one of those hostnames is also an
  inventory host fails naming it

### Requirement: Host and management names carry reverse records
Every host address and every distinct management address SHALL have one PTR record naming
its host or management name, in a reverse zone declared in site-config. Service and
instance names MUST NOT have PTR records.

#### Scenario: A reverse lookup names the host
- WHEN a host's address is queried as a PTR against the DNS server
- THEN the answer is that host's `<hostname>.host.<site>.<zone>` name and nothing else

### Requirement: NetBox and the inventory agree on every host name
NetBox SHALL record `<hostname>.host.<site>.<zone>` as the `dns_name` of each host address
and `<hostname>.mgmt.<site>.<zone>` for each distinct management address. A reconcile
playbook SHALL compare every declared host and management name and address with NetBox's
IP-address records, fail naming each disagreement, and write to neither side.

#### Scenario: A NetBox and inventory mismatch fails
- WHEN a declared host's address in NetBox carries a different `dns_name`, or the
  declared address is absent from NetBox
- THEN the reconcile fails naming the address and both values, and neither NetBox nor the
  zone is changed

#### Scenario: Agreement passes
- WHEN every declared host and management name and address matches a NetBox IP-address
  record
- THEN the reconcile passes and reports the number of names compared

### Requirement: Internal certificates carry the names clients verify
A member's server leaf SHALL carry its instance name and its service name as SANs. A
load balancer's leaf SHALL also carry every service name whose record is a CNAME to it.
A consumer that verifies an upstream by name MUST use the upstream's service name.

#### Scenario: Leaf SANs follow the names
- WHEN the vLLM server leaf for instance `dgx01` of `vllm-primary` and the gateway server
  leaf are declared
- THEN the vLLM leaf's SANs are `dgx01.vllm-primary.<site>.<zone>` and
  `vllm-primary.<site>.<zone>`, the gateway leaf's SANs include
  `vm01.gateway.<site>.<zone>`, `gateway.<site>.<zone>` and `inference.<site>.<zone>`, and
  the gateway's model `tls.hostname` is `vllm-primary.<site>.<zone>`
