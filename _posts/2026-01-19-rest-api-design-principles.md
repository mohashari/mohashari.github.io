---
layout: post
title: "REST API Design Principles That Stand the Test of Time"
tags: [api, backend, rest]
description: "Practical REST API design principles that make your APIs intuitive, maintainable, and developer-friendly."
---

A good REST API is a joy to use. A bad one is a daily source of frustration. After building and consuming dozens of APIs, here are the principles that consistently produce great results.

## 1. Use Nouns, Not Verbs for Resources

Resources are things, not actions. The HTTP method already carries the verb.

```
# Bad
GET /getUsers
POST /createUser
DELETE /deleteUser/42

# Good
GET    /users
POST   /users
DELETE /users/42
```

## 2. Use Plural Nouns Consistently

Pick plural and stick with it everywhere:

```
/users           # collection
/users/42        # single resource
/users/42/posts  # nested resource
```

## 3. HTTP Methods Have Meaning — Use Them Right

| Method | Use for | Idempotent? | Safe? |
|--------|---------|-------------|-------|
| GET | Read | Yes | Yes |
| POST | Create | No | No |
| PUT | Full replace | Yes | No |
| PATCH | Partial update | No | No |
| DELETE | Delete | Yes | No |

Use `PATCH` for partial updates, `PUT` for full replacement. Don't use `POST` for everything.

## 4. Status Codes Are Not Optional

Return meaningful HTTP status codes — don't always return `200 OK`:

```
200 OK          — Success, returning data
201 Created     — Resource created (POST)
204 No Content  — Success, nothing to return (DELETE)
400 Bad Request — Client sent invalid data
401 Unauthorized — Authentication required
403 Forbidden    — Authenticated but not authorized
404 Not Found    — Resource doesn't exist
409 Conflict     — State conflict (duplicate, etc.)
422 Unprocessable Entity — Validation failed
429 Too Many Requests    — Rate limited
500 Internal Server Error — Your bug
```

## 5. Consistent Error Response Shape

Always return errors in a predictable format:

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "Request validation failed",
    "details": [
      {
        "field": "email",
        "message": "Must be a valid email address"
      }
    ]
  }
}
```

## 6. Version Your API From Day One

```
/api/v1/users
/api/v2/users
```

Or use headers:
```
Accept: application/vnd.myapp.v2+json
```

URI versioning is simpler and more visible — prefer it.

## 7. Pagination, Filtering, and Sorting

Never return unbounded collections. Use cursor-based or offset pagination:

```
# Offset pagination
GET /users?page=2&per_page=20

# Cursor pagination (better for large datasets)
GET /users?cursor=eyJpZCI6MTAwfQ&limit=20
```

Include pagination metadata in the response:

```json
{
  "data": [...],
  "meta": {
    "total": 1543,
    "page": 2,
    "per_page": 20,
    "next_cursor": "eyJpZCI6MTIwfQ"
  }
}
```

Support filtering and sorting:

```
GET /users?status=active&role=admin&sort=created_at&order=desc
```

## 8. Use Proper Content Negotiation

```
Content-Type: application/json
Accept: application/json
```

Return `415 Unsupported Media Type` if the client sends an unsupported format.

## 9. Design for Idempotency

POST is not idempotent — retries can create duplicates. Use an idempotency key:

```
POST /payments
Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000

{
  "amount": 9900,
  "currency": "USD"
}
```

Store the key and return the same response on duplicate requests.

## 10. Document with OpenAPI

A well-documented API is 10x easier to use. Write an OpenAPI spec:

```yaml
openapi: 3.1.0
info:
  title: My API
  version: 1.0.0
paths:
  /users:
    get:
      summary: List all users
      parameters:
        - name: page
          in: query
          schema:
            type: integer
      responses:
        '200':
          description: Success
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/UserList'
```

Use [Swagger UI](https://swagger.io/tools/swagger-ui/) or [Redoc](https://github.com/Redocly/redoc) to generate interactive docs.

## Quick Checklist

- [ ] Resources are nouns, plural
- [ ] HTTP methods used correctly
- [ ] Meaningful status codes returned
- [ ] Consistent error format
- [ ] API versioned
- [ ] Collections paginated
- [ ] Idempotency keys for POST
- [ ] OpenAPI spec maintained

Good API design is an investment that pays dividends every day your team (and your users) work with it.
