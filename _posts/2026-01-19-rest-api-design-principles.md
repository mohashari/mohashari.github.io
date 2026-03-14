---
layout: post
title: "REST API Design Principles That Stand the Test of Time"
tags: [api, backend, rest]
description: "Practical REST API design principles that make your APIs intuitive, maintainable, and developer-friendly."
---

A good REST API is a joy to use. A bad one is a daily source of frustration. After building and consuming dozens of APIs, here are the principles that consistently produce great results.

## 1. Use Nouns, Not Verbs for Resources

Resources are things, not actions. The HTTP method already carries the verb.


<script src="https://gist.github.com/mohashari/bc69361c5aff9beb588ae4d5c0956ad0.js?file=snippet.txt"></script>


## 2. Use Plural Nouns Consistently

Pick plural and stick with it everywhere:


<script src="https://gist.github.com/mohashari/bc69361c5aff9beb588ae4d5c0956ad0.js?file=snippet-2.txt"></script>


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


<script src="https://gist.github.com/mohashari/bc69361c5aff9beb588ae4d5c0956ad0.js?file=snippet-3.txt"></script>


## 5. Consistent Error Response Shape

Always return errors in a predictable format:


<script src="https://gist.github.com/mohashari/bc69361c5aff9beb588ae4d5c0956ad0.js?file=snippet.json"></script>


## 6. Version Your API From Day One


<script src="https://gist.github.com/mohashari/bc69361c5aff9beb588ae4d5c0956ad0.js?file=snippet-4.txt"></script>


Or use headers:

<script src="https://gist.github.com/mohashari/bc69361c5aff9beb588ae4d5c0956ad0.js?file=snippet-5.txt"></script>


URI versioning is simpler and more visible — prefer it.

## 7. Pagination, Filtering, and Sorting

Never return unbounded collections. Use cursor-based or offset pagination:


<script src="https://gist.github.com/mohashari/bc69361c5aff9beb588ae4d5c0956ad0.js?file=snippet-6.txt"></script>


Include pagination metadata in the response:


<script src="https://gist.github.com/mohashari/bc69361c5aff9beb588ae4d5c0956ad0.js?file=snippet-2.json"></script>


Support filtering and sorting:


<script src="https://gist.github.com/mohashari/bc69361c5aff9beb588ae4d5c0956ad0.js?file=snippet-7.txt"></script>


## 8. Use Proper Content Negotiation


<script src="https://gist.github.com/mohashari/bc69361c5aff9beb588ae4d5c0956ad0.js?file=snippet-8.txt"></script>


Return `415 Unsupported Media Type` if the client sends an unsupported format.

## 9. Design for Idempotency

POST is not idempotent — retries can create duplicates. Use an idempotency key:


<script src="https://gist.github.com/mohashari/bc69361c5aff9beb588ae4d5c0956ad0.js?file=snippet-9.txt"></script>


Store the key and return the same response on duplicate requests.

## 10. Document with OpenAPI

A well-documented API is 10x easier to use. Write an OpenAPI spec:


<script src="https://gist.github.com/mohashari/bc69361c5aff9beb588ae4d5c0956ad0.js?file=snippet.yaml"></script>


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
