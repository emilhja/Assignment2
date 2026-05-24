# Docker Important Reminder

The Docker container does not automatically see changes made to the Python source files on the host.

The `Dockerfile` copies the project into the image with:

```dockerfile
COPY . .
```

The `docker-compose.yml` only mounts:

```yaml
./workspace:/workspace
```

That means files like `agent.py`, `safety.py`, `parser.py`, `tools.py`, and `tests/*` are baked into the image at build time. If you edit those files, an already-built image may still run old code.

## After Editing Code

Run:

```bash
docker compose build agent
docker compose run --rm agent
```

## Before Final Testing Or Demo

Use a clean rebuild to be extra sure:

```bash
docker compose build --no-cache agent
docker compose run --rm agent python -m pytest -q
docker compose run --rm agent
```

Expected current test result:

```text
23 passed
```

## Why This Matters

If Docker shows only the old 5 tests, or the agent repeats old unsafe behavior, the container is probably running an old image. Rebuild the image before judging the latest code.
