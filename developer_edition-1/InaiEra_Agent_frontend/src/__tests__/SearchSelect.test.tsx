/**
 * Monitoring's agent filter was a native <select> holding every agent.
 *
 * With 27 of them it opened as a single unbroken column that covered
 * most of the page, offered no way to narrow it, and could only be
 * walked with a scrollbar. This is the replacement: height-capped,
 * filterable, and driveable entirely from the keyboard.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { useState } from 'react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import SearchSelect from '../components/SearchSelect';

const OPTIONS = Array.from({ length: 27 }, (_, i) => ({
  value: `ag${i}`, label: `Agent ${i}`,
}));

function Harness({ onChange }: { onChange?: (v: string) => void }) {
  const [value, setValue] = useState('ag0');
  return (
    <>
      <label id="lbl" htmlFor="sel">Filter by agent</label>
      <SearchSelect id="sel" labelledBy="lbl" noun="agent" value={value}
        options={OPTIONS} onChange={(v) => { setValue(v); onChange?.(v); }} />
    </>
  );
}

const open = () => fireEvent.click(screen.getByLabelText('Filter by agent'));
const list = () => screen.getByRole('listbox');
const active = () => list().querySelector('[data-active="true"]')?.textContent;

describe('SearchSelect', () => {
  beforeEach(() => vi.clearAllMocks());

  it('stays closed until asked, showing only the selection', () => {
    render(<Harness />);
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(screen.getByLabelText('Filter by agent').textContent).toContain('Agent 0');
  });

  it('caps the list height instead of letting 27 names run down the page', () => {
    render(<Harness />);
    open();
    // The cap lives on the scrolling list, not the panel, so the search
    // box and the footer stay put while the names move under them.
    expect(list().className).toContain('ss-list');
    expect(screen.getAllByRole('option')).toHaveLength(27);
  });

  it('floats over the page rather than pushing it down', () => {
    render(<Harness />);
    open();
    const menu = list().closest('.ss-menu') as HTMLElement | null;
    expect(menu).toBeTruthy();
    // Fixed and portalled to <body>, so it floats over drawers and cards instead of
    // pushing the page down (or being clipped by an overflow:hidden ancestor).
    expect(menu!.style.position).toBe('fixed');
    expect(menu!.parentElement).toBe(document.body);
    // Asserted against the stylesheet rather than getComputedStyle:
    // nothing loads styles.css under jsdom, so every element there
    // reports position:static and the check would pass on any markup.
    // In-flow, every card below the filter jumps down on each open.
    const css = readFileSync(resolve(process.cwd(), 'src/components/search-select.css'), 'utf-8');
    expect(css).toContain('.ss-list{max-height:');
  });

  it('walks the names with the arrow keys and takes one with Enter', () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    open();
    // Opening lands on what is already selected, so Down is the NEXT
    // agent rather than a jump back to the top of the list.
    expect(active()).toContain('Agent 0');
    const box = screen.getByLabelText('Type to filter…');
    fireEvent.keyDown(box, { key: 'ArrowDown' });
    fireEvent.keyDown(box, { key: 'ArrowDown' });
    expect(active()).toContain('Agent 2');
    fireEvent.keyDown(box, { key: 'ArrowUp' });
    expect(active()).toContain('Agent 1');
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith('ag1');
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('clamps at both ends rather than wrapping around', () => {
    render(<Harness />);
    open();
    const box = screen.getByLabelText('Type to filter…');
    fireEvent.keyDown(box, { key: 'ArrowUp' });
    // Already on the first row; wrapping to the last would read as a glitch.
    expect(active()).toContain('Agent 0');
    fireEvent.keyDown(box, { key: 'End' });
    expect(active()).toContain('Agent 26');
    fireEvent.keyDown(box, { key: 'ArrowDown' });
    expect(active()).toContain('Agent 26');
  });

  it('gives the same two steps to a mouse', () => {
    render(<Harness />);
    open();
    fireEvent.click(screen.getByLabelText('Next agent'));
    fireEvent.click(screen.getByLabelText('Next agent'));
    expect(active()).toContain('Agent 2');
    fireEvent.click(screen.getByLabelText('Previous agent'));
    expect(active()).toContain('Agent 1');
    // No further up to go from the first row.
    fireEvent.click(screen.getByLabelText('Previous agent'));
    expect(screen.getByLabelText('Previous agent')).toBeDisabled();
  });

  it('filters by typing, and says how much of the list is left', () => {
    render(<Harness />);
    open();
    const box = screen.getByLabelText('Type to filter…');
    // Walked down first, so a highlight left where it was lands on a
    // different row than the one the reset should produce — otherwise
    // both behaviours agree and the assertion proves nothing.
    for (let i = 0; i < 5; i++) fireEvent.keyDown(box, { key: 'ArrowDown' });
    expect(active()).toContain('Agent 5');
    fireEvent.change(box, { target: { value: 'agent 1' } });
    // 1, 10–19.
    expect(screen.getAllByRole('option')).toHaveLength(11);
    expect(screen.getByText('11 of 27')).toBeTruthy();
    // The highlight moves to the first match: the selected row is
    // usually not in the filtered list at all, and Enter must not take
    // something that is off screen.
    expect(active()).toBe('Agent 1');
  });

  it('says when nothing matches instead of showing an empty box', () => {
    render(<Harness />);
    open();
    fireEvent.change(screen.getByLabelText('Type to filter…'), { target: { value: 'zzz' } });
    expect(screen.getByText('Nothing matches')).toBeTruthy();
    // Enter on an empty list must do nothing rather than throw.
    fireEvent.keyDown(screen.getByLabelText('Type to filter…'), { key: 'Enter' });
    expect(screen.getByRole('listbox')).toBeTruthy();
  });

  it('Escape closes without changing the selection, and drops the filter', () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    open();
    fireEvent.change(screen.getByLabelText('Type to filter…'), { target: { value: 'agent 5' } });
    fireEvent.keyDown(screen.getByLabelText('Type to filter…'), { key: 'Escape' });
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
    // A stale filter on the next open would hide most of the list with
    // no visible cause.
    open();
    expect(screen.getAllByRole('option')).toHaveLength(27);
  });

  it('closes when the click lands somewhere else', () => {
    render(<Harness />);
    open();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('marks the current selection so it is findable in a long list', () => {
    render(<Harness />);
    open();
    const chosen = screen.getAllByRole('option').filter(
      (o) => o.getAttribute('aria-selected') === 'true');
    expect(chosen).toHaveLength(1);
    expect(within(chosen[0]).getByText('Agent 0')).toBeTruthy();
  });
});
