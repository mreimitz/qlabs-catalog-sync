import { useId, useState, type FormEvent } from "react";
import {
  Alert,
  AlertDescription,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  FieldRow,
  Input,
  Spinner,
} from "@elabs-ai/components-ui";
import { AppIcon } from "@elabs-ai/components-icons";

import { signIn } from "./authApi";

/** Sign-in (C7: single administrator). Rendered by `AuthGate` whenever the session store is
 * `"signed-out"` -- on first boot with no cookie, or any time later that a 401 clears the
 * session.
 *
 * A real `<form>` (Enter submits, a screen reader announces the region as a form) using
 * `FieldRow` for label/control association: `Form`/`FormField` (`@elabs-ai/components-ui`)
 * need a `react-hook-form` tree, and `react-hook-form` is not one of this console's
 * dependencies (it is only a *transitive* dependency of `components-ui`'s own build, not
 * resolvable from this app's code) -- see the T13.2 report. `FieldRow` is the library's own
 * answer for a field outside `react-hook-form`, and two `useState` fields do not need more.
 *
 * The failure message is the ONE shape `auth.py`'s `ConsoleAuth.sign_in` returns for "wrong
 * username", "wrong password" and "wrong both" -- rendered once, above both fields, and
 * deliberately never attached to a specific `FieldRow`'s own `error` prop, so nothing here
 * invents a finer-grained message the server didn't give it.
 */
export function SignInScreen() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const headingId = useId();

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setErrorMessage(null);
    const result = await signIn(username, password);
    setPending(false);
    if (!result.ok) {
      setErrorMessage(result.error?.message ?? "Invalid username or password.");
    }
  }

  return (
    <div className="flex min-h-dvh items-center justify-center bg-background p-6">
      <Card className="w-full max-w-sm">
        <CardHeader className="items-center gap-2 text-center">
          <AppIcon title="QLabs Catalog Sync" height={28} />
          <CardTitle as="h1" id={headingId}>
            Sign in
          </CardTitle>
          <CardDescription>Operate the Databricks-to-Qlik sync engine.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={(event) => void handleSubmit(event)} aria-labelledby={headingId} noValidate>
            <div className="flex flex-col gap-4">
              {errorMessage ? (
                <Alert variant="destructive">
                  <AlertDescription>{errorMessage}</AlertDescription>
                </Alert>
              ) : null}
              <FieldRow label="Username">
                <Input
                  name="username"
                  autoComplete="username"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  autoFocus
                  required
                />
              </FieldRow>
              <FieldRow label="Password">
                <Input
                  name="password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                />
              </FieldRow>
              <Button type="submit" disabled={pending} className="mt-2">
                {pending ? <Spinner aria-hidden className="mr-2 size-4" /> : null}
                {pending ? "Signing in…" : "Sign in"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
