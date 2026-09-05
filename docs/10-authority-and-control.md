# Atlas V5 Authority and Control

## Authority principle

Atlas should enforce authority at the point where an effect occurs, using the simplest trustworthy boundary available.

The model may decide what action is useful. It does not decide whether it has permission to exceed the owner's configured authority.

## Local authority

For machine-local work, the Atlas service identity and ordinary operating-system controls should carry as much of the real boundary as practical:

- filesystem permissions;
- executable permissions;
- service/socket access;
- user/group membership;
- container or sandbox boundaries where useful.

Atlas should not duplicate an OS restriction with a second elaborate policy engine unless there is a product-level reason to do so.

## External authority

Gmail, Drive, GitHub, databases, and other services retain their own authentication, OAuth scopes, tokens, account roles, and service policies.

Atlas stores and refreshes credentials locally where practical, but possession of a credential does not require Atlas to invent another workflow around it.

## Thin owner policy

Some consequential effects may deserve an Atlas-level standing rule or confirmation even when the underlying credential permits them.

Examples may include sending external communications, destructive deletion, publishing, financial actions, or changes to Atlas's own authority. The exact list is a design decision, not assumed here.

The policy should answer a narrow question: may Atlas perform this effect under the current owner rule? It should not decide the workflow leading to that effect.
## Failure feedback

A denied action should return a precise technical reason to the model and owner. Atlas should distinguish policy denial, OS permission denial, expired authentication, unavailable service, invalid resource, and provider/tool failure.

That distinction lets the model reason about the next useful move instead of entering a generic blocked state.

## Control surface

Control is the owner's engineering panel over Atlas. It is primarily for configuration, observability, diagnosis, and exceptional intervention.

Potential areas include:

- providers, models, routing, and reasoning effort;
- credentials and connected accounts;
- MCP servers and discovered capability families;
- local software/tool availability;
- authority and confirmation rules;
- schedules and automation status;
- memory inspection/correction/forgetting;
- active workspaces and recent activity;
- model/tool usage, latency, token counts, and estimated/reconciled cost;
- errors, authentication health, and service state.

The normal task path should not require visiting Control.

## Explainability target

When something goes wrong, the owner should be able to answer: which model was in the seat, what context/workspace was active, what it tried, which tool boundary handled the action, what permission applied, what exact result came back, and what Atlas did next.

Observability should explain the machine without forcing the owner to operate the machine manually.
