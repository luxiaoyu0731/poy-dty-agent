import { Children, useId, useRef, useState, type ReactNode } from "react";

/** Keeps each panel mounted so switching tabs preserves queries and scroll positions. */
export default function WorkbenchTabs({ labels, children, className = "", onChange }: {
  labels: string[]; children: ReactNode; className?: string; onChange?: (index: number) => void;
}) {
  const [active, setActive] = useState(0);
  const id = useId();
  const select = (index: number) => { setActive(index); onChange?.(index); };
  const buttons = useRef<Array<HTMLButtonElement | null>>([]);
  return <section className={`review-tabs ${className}`}>
    <div className="review-tabbar" role="tablist">
      {labels.map((name, index) => <button key={name} type="button" role="tab"
        ref={element => { buttons.current[index] = element; }}
        id={`${id}-tab-${index}`} aria-controls={`${id}-panel-${index}`}
        aria-selected={active === index} tabIndex={active === index ? 0 : -1}
        onClick={() => select(index)} onKeyDown={event => {
          let next = index;
          if (event.key === "ArrowRight") next = (index + 1) % labels.length;
          else if (event.key === "ArrowLeft") next = (index + labels.length - 1) % labels.length;
          else if (event.key === "Home") next = 0;
          else if (event.key === "End") next = labels.length - 1;
          else return;
          event.preventDefault(); select(next); buttons.current[next]?.focus();
        }}>{name}</button>)}
    </div>
    {Children.toArray(children).filter(child => typeof child !== "string" || child.trim().length > 0).map((child, index) => <div key={index}
      id={`${id}-panel-${index}`} aria-labelledby={`${id}-tab-${index}`}
      className="review-tab-content" hidden={index !== active} role="tabpanel">{child}</div>)}
  </section>;
}
