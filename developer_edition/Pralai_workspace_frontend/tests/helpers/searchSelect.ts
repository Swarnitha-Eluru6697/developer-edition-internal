/** Drive the shared SearchSelect dropdown (src/shared/ui/SearchSelect.tsx) from a test. */
import { act } from "react";

/** The labels the dropdown offers; opens it if needed (the panel is portalled to <body>). */
export async function searchSelectOptions(trigger: HTMLElement): Promise<string[]> {
  if (trigger.getAttribute("aria-expanded") !== "true") await act(async () => { trigger.click(); });
  return [...document.body.querySelectorAll<HTMLElement>("[role='option'] .ss-option-label")].map((o) => o.textContent ?? "");
}

/** Open the dropdown and click the option whose label is `label`. */
export async function pickSearchSelect(trigger: HTMLElement, label: string): Promise<void> {
  if (trigger.getAttribute("aria-expanded") !== "true") await act(async () => { trigger.click(); });
  const option = [...document.body.querySelectorAll<HTMLElement>("[role='option']")]
    .find((o) => o.querySelector(".ss-option-label")?.textContent === label);
  if (!option) throw new Error(`SearchSelect has no option "${label}"`);
  await act(async () => { option.click(); });
}
