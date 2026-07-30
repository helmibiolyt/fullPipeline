# Deploying

The repo is the source of truth. Nothing a host needs lives only on that host,
so replacing a VM is `git clone` plus one script — not archaeology through
shell history.

## The rule

    edit here  ->  git push  ->  git pull on the host  ->  re-run its script

Provisioning scripts are idempotent, because the realistic use is not "build a
fresh VM" but "the VM drifted, put it back". A script you are afraid to re-run
is a script nobody runs.

## Two hosts

| host | runs | provision with |
|------|------|----------------|
| vector | Qdrant, embedding/ingest, search API, Airflow | `deploy/vector-host.sh` |
| graph  | graph build, Neo4j | `deploy/graph-host.sh` |

They are separate because their resource profiles conflict. Qdrant wants its
vectors resident; Neo4j wants its store resident; the graph build wants ~6 GB
for a while. Two of those on one 16 GB box means whichever is largest gets
paged out, and a swapped page cache is worse than a small one.

## Standing up a host from nothing

    git clone https://github.com/helmibiolyt/fullPipeline.git
    cd fullPipeline

    # credentials are NOT in the repo - put them in place first
    #   preferred: an IAM instance role (AWS). Azure cannot do this, so:
    cp /somewhere/safe/.env automation/.env && chmod 600 automation/.env

    bash deploy/vector-host.sh        # or graph-host.sh

## Operating the graph

    bash deploy/build-graph.sh                    # ~30 min, builds + validates
    NEO4J_PASSWORD='...' bash deploy/import-graph.sh

`import-graph.sh` refuses to run against a build with no passing validation.
Neo4j has no transaction around a bulk import — it replaces the store outright
— so an unchecked build silently becomes the live graph, and the failure mode
is a confident wrong answer rather than an error.

Builds land in `~/graph-runs/`, outside the repo. Two are kept: the previous
one is what you re-import from when a new build validates but turns out wrong
for a reason `validate.py` cannot see.

## Operating the vector store

    bash deploy/airflow.sh up
    ~/vsenv/bin/python vector_store/ingest.py --prune

Ingest is incremental by S3 ETag, so re-running is cheap. `--prune` removes
vectors for documents that have left S3; without it retrieval keeps citing
documents that no longer exist, which reads as a well-sourced answer to
something untrue.

## What these scripts deliberately do not do

**Open firewalls.** Neo4j listens on 0.0.0.0 because it has to be reachable,
and Qdrant on 6333 must not be. Which addresses may reach them is a
security-group / NSG rule — not something a script on the box can set, and not
something to default open.

**Hold secrets.** `automation/.env` is gitignored and stays that way. On AWS
prefer an instance role: nothing to rotate, nothing to leak, and it survives
the box being replaced. Azure cannot do this, so that host needs a key file.

**Ingest or build automatically.** Both are long and expensive. Provisioning
gets a host ready; running the work is a separate, explicit command.
