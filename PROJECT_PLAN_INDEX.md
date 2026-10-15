# Autosport — Canonical Multi-Plan Index

## Authority
The former monolithic 46-Section execution plan is SUPERSEDED FOR WORK SELECTION and remains audit-only.

Canonical Drive folder:
https://drive.google.com/drive/folders/1zg2OGLpc5J3m4K-qKLuWsf2pkm6yircx

Plans 1–6 are independent engineering plans with no priority order.
Plan 7 is provider-free PAPER/SHADOW whole-product convergence.
Plan 8 is the final dependency-aware external-bookmaker/real-execution/NVDA/release plan.

## File/folder naming invariant

Inside the project Drive root there is exactly one canonical subfolder named **`Проєктні плани`**.
The plan file names are deliberately identical across projects and contain **no thematic suffix**:

1. `1. Перший план`
2. `2. Другий план`
3. `3. Третій план`
4. `4. Четвертий план`
5. `5. П’ятий план`
6. `6. Шостий план`
7. `7. Сьомий план`
8. `8. Восьмий план`

The thematic scope lives **inside** each document and in this index. Workers MUST NOT rename the Drive files to include subsystem names.

## Plans
1. Drive file: `1. Перший план`
   Internal scope: Дані, причинність, зберігання, replay та outcomes
https://docs.google.com/document/d/1uNEWkGK3_HNb_u6uZz4vUPap4uJeSxaO_BA0iQrgIuY/edit

2. Drive file: `2. Другий план`
   Internal scope: Фінанси, ризик, PaperBook, портфель і scenario truth
https://docs.google.com/document/d/1XQmujyDGfHXe8_ohumnoivS6bSAMNdTKOUOu9wYjKG8/edit

3. Drive file: `3. Третій план`
   Internal scope: Intelligence, research, learning, models та multi-sport
https://docs.google.com/document/d/1hIu9FQmNLR4KGhVciURT-GH49QDHFnKv_EE9c3xOvtU/edit

4. Drive file: `4. Четвертий план`
   Internal scope: Bookmakers, provider adapters та execution engineering
https://docs.google.com/document/d/1wEC5a82Vf5c_66t4upGQm4X215IysCNwkwdvfN6ee40/edit

5. Drive file: `5. П’ятий план`
   Internal scope: Runtime, security, recovery, QA та performance
https://docs.google.com/document/d/18lEAZZoktPOWjx8SUs6lFc_mY-n8fvYI371PdvZ1Axw/edit

6. Drive file: `6. Шостий план`
   Internal scope: Windows, accessibility, packaging та product UI
https://docs.google.com/document/d/1qWTi8eG9SDrNWvtUoYmJyIl92ySviTBRympJ3jvvJVo/edit

7. Drive file: `7. Сьомий план`
   Internal scope: Provider-free PAPER/SHADOW whole-product convergence
https://docs.google.com/document/d/19ifIaWknBedZBodtwjs3dG7uqVA_39hduUzox0A6P_Q/edit

8. Drive file: `8. Восьмий план`
   Internal scope: External bookmaker qualification, real execution, NVDA та final release
https://docs.google.com/document/d/1DmEXcaIF4nff8iNLKFsV6CxexX1v7cEq8E8FCRq5bX8/edit

## Worker selection
- Plans 1–6 may start and finish in any order.
- Use MULTI_PLAN_CLOSURE_STATE.md as live status authority; Drive statuses are migration snapshots.
- Existing PR/branch/source must be REUSE -> REPAIR -> CONVERGE before duplicate scope.
- Plan 4 is actionable offline without bookmaker credentials/accounts using existing code, public/recorded data and fixtures.
- Plan 7 waits for terminal Plans 1,2,3,5,6 plus the provider-free qualification slice of Plan 4: Sections 1,2,7 (or a later equivalent with the same acceptance boundary). The rest of Plan 4—supervised/real execution machinery—does not block M1 and continues in parallel toward Plan 8.
- Plan 8 uses per-Section external gates; absence of bookmaker/account evidence never blocks Plans 1–7.
- Old sequential numbering no longer chooses work.

Detailed migration: LEGACY_46_TO_MULTIPLAN_COVERAGE.md.
