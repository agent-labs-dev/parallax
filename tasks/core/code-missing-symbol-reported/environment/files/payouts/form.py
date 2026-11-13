from payouts.validation import validate_account_number, validate_payee_name, validate_sort_code


def errors(form: dict[str, str]) -> list[str]:
    problems = []
    if not validate_payee_name(form.get("payee", "")):
        problems.append("payee")
    if not validate_sort_code(form.get("sort_code", "")):
        problems.append("sort_code")
    if not validate_account_number(form.get("account", "")):
        problems.append("account")
    return problems
