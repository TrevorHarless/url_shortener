# URL Shortener

A simple URL shortener built with FastAPI, Postgres, and Redis. This was built just for fun, but core concepts could be expanded to a production-ready url shortener. 

Based on the following design (minus the gateway and microservices haha):
![Diagram](diagram.JPG)


- **Postgres** stores the short codes and the URLs they point to.
- **Redis** caches lookups so redirects skip the database when they can (entries expire after 1 hour).

## Running

Start Postgres and Redis:

```bash
docker compose up -d
```

Then start the API:

```bash
uv run uvicorn main:app --reload
```

The API runs at http://localhost:8000, with interactive docs at http://localhost:8000/docs.

| Service  | Host port |
| -------- | --------- | 
| API      | 8000      |
| Postgres | 5433      |
| Redis    | 6379      |

## Usage

Create a short URL:

```bash
curl -X POST http://localhost:8000/urls \
  -H "Content-Type: application/json" \
  -d '{"long_url": "https://trevorharless.com", "created_by": "trevor"}'
```

```json
{
  "short_code": "aB3xYz9",
  "long_url": "https://trevorharless.com/",
  "short_url": "http://localhost:8000/aB3xYz9",
  "created_at": "2026-11-14T18:00:00.000000Z",
  "created_by": "trevor"
}
```

`created_by` is optional.

Follow it (the API sends a redirect to the original URL):

```bash
curl -i http://localhost:8000/aB3xYz9
```

## Monitoring

### Redis

```bash
redis-cli monitor
```

Open a short URL and you'll see a `GET <short_code>`. On a cache miss it's followed by a `SET <short_code> <long_url> EX 3600` after the lookup in Postgres; a `GET` with no `SET` after it means the URL came from the cache. Creating a URL doesn't touch Redis. Press `Ctrl+C` to stop.

### Postgres

Open a `psql` shell inside the container:

```bash
docker compose exec postgres psql -U postgres -d shortener
```

Then run:

```sql
\dt                               
SELECT * FROM short_urls;         
```

Note: The Postgres container has no volume, so the data is lost when it's removed.
