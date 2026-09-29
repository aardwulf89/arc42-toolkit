# 5. Building Block View

## 5.1 Whitebox Overall System

| Name | Responsibility | Interfaces |
|------|----------------|------------|
| `Order Service` | Accepts and validates orders | `IF-01` |
| **Fulfilment Gateway** | Hands orders to the warehouse | [IF-02](#if-02) |
| Name Registry | Resolves service names | — |

## 5.2 Level 2

| Name | Responsibility |
|------|----------------|
| Order Validator | Checks order payloads |
