/**
 * Test helpers for the shared SearchSelect dropdown (it replaced every
 * native <select>). The trigger is a button; its panel is portalled to
 * document.body, so `screen` queries find the options.
 */
import { fireEvent, screen } from '@testing-library/react';

/** Open the dropdown whose trigger is `trigger`. */
export function openSelect(trigger: HTMLElement): void {
  if (trigger.getAttribute('aria-expanded') !== 'true') fireEvent.click(trigger);
}

/** Open the dropdown and click the option named `name`. */
export function pickOption(trigger: HTMLElement, name: string | RegExp): void {
  openSelect(trigger);
  fireEvent.click(screen.getByRole('option', { name }));
}

/** The visible options of the open dropdown, as their text. */
export function optionTexts(): string[] {
  return screen.getAllByRole('option').map((o) => o.textContent || '');
}
