/*
 * Select — DataConnect's dropdown: the platform's SearchSelect (the Catalog's and Workspace's, copied verbatim as SearchSelect.tsx /
 * search-select.css) behind the shape of a native <select>, so a form keeps its <option> children and its `onChange(e)` handler:
 *
 *   <Select id="pipe-zone" value={zone} onChange={(e) => setZone(e.target.value)}><option>raw</option><option>curated</option></Select>
 *
 * An <option> is { value: its value attribute, else its text; label: its text }. The id goes on the trigger button, so a <label htmlFor> names
 * it. SearchSelect has no props for aria-describedby / aria-invalid, so they are put on the trigger here. The trigger also carries
 * data-options ([{value,label,disabled}]) and data-value so a test can pick an option by its value and read the current one (tests/e2e/fixtures.ts → choose, optionsOf).
 */
import React, { useEffect, useMemo, useRef } from "react";
import SearchSelect, { type SelectOption } from "./SearchSelect";

export interface SelectChange { target: { value: string }; currentTarget: { value: string } }

interface Props {
  id?: string;
  value: string;
  onChange?: (e: SelectChange) => void;
  disabled?: boolean;
  className?: string;
  style?: React.CSSProperties;
  placeholder?: string;
  noun?: string;
  emptyText?: string;
  "aria-label"?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean;
  children?: React.ReactNode;
}

const textOf = (node: React.ReactNode): string => React.Children.toArray(node).map((c) => (typeof c === "string" || typeof c === "number" ? String(c) : "")).join("");

/** The <option> elements among the children, through fragments, arrays and conditionals. */
function optionsOf(children: React.ReactNode): SelectOption[] {
  const out: SelectOption[] = [];
  const walk = (nodes: React.ReactNode) => {
    React.Children.forEach(nodes, (child) => {
      if (!React.isValidElement(child)) return;
      const el = child as React.ReactElement<{ value?: string | number; disabled?: boolean; children?: React.ReactNode }>;
      if (el.type === React.Fragment) { walk(el.props.children); return; }
      if (el.type !== "option") return;
      const label = textOf(el.props.children);
      out.push({ value: el.props.value !== undefined ? String(el.props.value) : label, label, disabled: el.props.disabled });
    });
  };
  walk(children);
  return out;
}

export default function Select({
  id, value, onChange, disabled, className, style, placeholder, noun, emptyText, children,
  "aria-label": ariaLabel, "aria-describedby": describedBy, "aria-invalid": invalid,
}: Props) {
  const options = useMemo(() => optionsOf(children), [children]);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const trigger = rootRef.current?.querySelector<HTMLButtonElement>("button.ss-trigger");
    if (!trigger) return;
    const set = (name: string, v: string | undefined) => { if (v === undefined) trigger.removeAttribute(name); else trigger.setAttribute(name, v); };
    set("aria-describedby", describedBy);
    set("aria-invalid", invalid ? "true" : undefined);
    set("data-value", value);
    set("data-options", JSON.stringify(options.map(({ value: v, label, disabled: d }) => ({ value: v, label, disabled: !!d }))));
  });

  return (
    <div ref={rootRef} style={{ display: "contents" }}>
      <SearchSelect
        id={id} ariaLabel={ariaLabel} value={value} options={options} disabled={disabled} className={className} style={style}
        placeholder={placeholder} noun={noun} emptyText={emptyText}
        onChange={(v) => onChange?.({ target: { value: v }, currentTarget: { value: v } })}
      />
    </div>
  );
}
