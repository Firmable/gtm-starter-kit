/**
 * Structured errors raised by the Firmable client. Mirrors shared/python/src/firmable_client/errors.py.
 * `chargeUncertain` is true when the server may have charged for the request even though no usable
 * response came back, for example after a timeout or a 5xx on a billable endpoint.
 */

export type ErrorKind =
  | "error" | "bad_request" | "unauthorized" | "insufficient_credits" | "forbidden" | "not_found"
  | "rate_limited" | "server_error" | "unexpected_status" | "timeout" | "transport_error"
  | "redirect_blocked" | "invalid_response" | "budget_exceeded";

export interface ErrorDict {
  kind: ErrorKind;
  status: number | null;
  code: string | null;
  endpoint: string | null;
  message: string;
  charge_uncertain: boolean;
  retry_after: string | null;
}

export interface ErrorOptions {
  status?: number | null;
  code?: string | null;
  endpoint?: string | null;
  chargeUncertain?: boolean;
  retryAfter?: string | null;
}

export class FirmableError extends Error {
  kind: ErrorKind = "error";
  status: number | null;
  code: string | null;
  endpoint: string | null;
  chargeUncertain: boolean;
  retryAfter: string | null;

  constructor(message: string, opts: ErrorOptions = {}) {
    super(message);
    this.status = opts.status ?? null;
    this.code = opts.code ?? null;
    this.endpoint = opts.endpoint ?? null;
    this.chargeUncertain = opts.chargeUncertain ?? false;
    this.retryAfter = opts.retryAfter ?? null;
  }

  toDict(): ErrorDict {
    return {
      kind: this.kind,
      status: this.status,
      code: this.code,
      endpoint: this.endpoint,
      message: this.message,
      charge_uncertain: this.chargeUncertain,
      retry_after: this.retryAfter,
    };
  }
}

export class BadRequestError extends FirmableError { override kind: ErrorKind = "bad_request"; }
export class AuthenticationError extends FirmableError { override kind: ErrorKind = "unauthorized"; }
export class InsufficientCreditsError extends FirmableError { override kind: ErrorKind = "insufficient_credits"; }
export class ForbiddenError extends FirmableError { override kind: ErrorKind = "forbidden"; }
export class NotFoundError extends FirmableError { override kind: ErrorKind = "not_found"; }
export class RateLimitedError extends FirmableError { override kind: ErrorKind = "rate_limited"; }
export class ServerError extends FirmableError { override kind: ErrorKind = "server_error"; }
export class UnexpectedStatusError extends FirmableError { override kind: ErrorKind = "unexpected_status"; }
export class TransportTimeoutError extends FirmableError { override kind: ErrorKind = "timeout"; }
export class TransportError extends FirmableError { override kind: ErrorKind = "transport_error"; }
export class RedirectBlockedError extends FirmableError { override kind: ErrorKind = "redirect_blocked"; }
export class InvalidResponseError extends FirmableError { override kind: ErrorKind = "invalid_response"; }
/** Raised before a request is sent when its reservation would exceed the credit limit. */
export class BudgetExceededError extends FirmableError { override kind: ErrorKind = "budget_exceeded"; }

/** Invalid recipe configuration or command-line arguments. Raised before any request. */
export class ConfigError extends Error {}
/** Invalid input file. Raised before any request. */
export class InputError extends Error {}

/**
 * Maps an HTTP status to the matching error class. 4xx means the request was refused, so nothing
 * was charged. A 5xx on a billable endpoint is marked charge-uncertain.
 */
export function errorForStatus(status: number, message: string, opts: {
  code: string | null; endpoint: string; billable: boolean; retryAfter: string | null;
}): FirmableError {
  const base = { status, code: opts.code, endpoint: opts.endpoint };
  if (status === 400) return new BadRequestError(message, base);
  if (status === 401) return new AuthenticationError(message, base);
  if (status === 402) return new InsufficientCreditsError(message, base);
  if (status === 403) return new ForbiddenError(message, base);
  if (status === 404) return new NotFoundError(message, base);
  if (status === 429) return new RateLimitedError(message, { ...base, retryAfter: opts.retryAfter });
  if (status >= 500 && status <= 599) return new ServerError(message, { ...base, chargeUncertain: opts.billable });
  return new UnexpectedStatusError(message, { ...base, chargeUncertain: opts.billable });
}
