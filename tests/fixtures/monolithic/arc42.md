# Architecture Documentation: Shop

# 1. Introduction and Goals

## 1.2 Quality Goals

| Priority | Quality Goal | Scenario |
|----------|--------------|----------|
| 1 | #reliable Reliability | Orders survive the loss of one node |

# 3. Context and Scope

| ID | Partner | Protocol |
|----|---------|----------|
| IF-01 | Payment Provider | HTTPS |
| IF-02 | Warehouse | AMQP |

# 5. Building Block View

## 5.1 Whitebox Overall System

| Name | Responsibility | Interfaces |
|------|----------------|------------|
| Order Service | Accepts orders | IF-01 |
| Gateway (v2) | Routes traffic | IF-03 |
| Label Printer | Prints shipping labels | — |

## 5.2 Level 2

| Name | Responsibility |
|------|----------------|
| Order Validator | Checks order payloads |

# 7. Deployment View

```bash
# 9. Not a heading — a shell comment inside a code block
kubectl apply -f deploy/
```

The Order
Service and the Gateway (v2) run in the production cluster.

![Production](diagrams/deployment-prod.puml)

# 10. Quality Requirements

### QS-10: Node failure

| Field | Value |
|-------|-------|
| **Quality property** | #reliable |

## 10.3 Aspirational Scenarios

| ID | Scenario | Current State | Target |
|----|----------|---------------|--------|
| QS-1 | Checkout latency | not measured | 300 ms |
| QS-10 | Node failure | not measured | 99.9 % |

# 11. Risks and Technical Debt

| ID | Title |
|----|-------|
| RISK-01 | QS-10 failover is unverified |
