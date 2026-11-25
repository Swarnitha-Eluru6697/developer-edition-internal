import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import type { Agent } from '../types';

vi.mock('../api', () => ({ api: { getAgent: vi.fn(), runAgent: vi.fn() } }));
import { api } from '../api';
import AgentRunner from '../pages/AgentRunner';

const BASE: Agent = {
  id: 'a1', name: 'Fallback Agent', app_type: 'chat', status: 'published',
  flow: { nodes: [], edges: [] }, features: {},
} as unknown as Agent;

function mount(agent: Agent) {
  (api.getAgent as ReturnType<typeof vi.fn>).mockResolvedValue(agent);
  return render(
    <MemoryRouter initialEntries={['/app/a1']}>
      <Routes><Route path="/app/:agentId" element={<AgentRunner />} /></Routes>
    </MemoryRouter>,
  );
}

describe('web app settings reach the published app', () => {
  beforeEach(() => vi.clearAllMocks());

  it('falls back to the agent name when no settings are saved', async () => {
    mount(BASE);
    expect(await screen.findByText('Fallback Agent')).toBeTruthy();
  });

  it('applies display name, icon, description, theme, accent, placeholder and legal footer', async () => {
    const { container } = mount({
      ...BASE,
      features: {
        webapp_settings: {
          display_name: 'Compliance Desk',
          description: 'Answers questions about internal controls.',
          icon: '🛡️',
          theme: 'dark',
          color: '#B5650A',
          input_placeholder: 'Ask about a control…',
          copyright: '© 2026 Aadhya Analytics',
          privacy_policy_url: 'https://example.com/privacy',
          terms_of_service_url: 'https://example.com/terms',
        },
      },
    } as unknown as Agent);

    // Identity replaces the agent's internal name.
    expect(await screen.findByText('Compliance Desk')).toBeTruthy();
    expect(screen.queryByText('Fallback Agent')).toBeNull();
    expect(screen.getByText('Answers questions about internal controls.')).toBeTruthy();
    expect(screen.getByText('🛡️')).toBeTruthy();

    // Theme and accent.
    const shell = container.querySelector('.runner-shell') as HTMLElement;
    expect(shell.classList.contains('runner-shell-dark')).toBe(true);
    expect(shell.style.getPropertyValue('--teal')).toBe('#B5650A');

    // Placeholder.
    await waitFor(() => expect(screen.getByPlaceholderText('Ask about a control…')).toBeTruthy());

    // Legal footer.
    expect(screen.getByText('© 2026 Aadhya Analytics')).toBeTruthy();
    expect(screen.getByText('Privacy').getAttribute('href')).toBe('https://example.com/privacy');
    expect(screen.getByText('Terms').getAttribute('href')).toBe('https://example.com/terms');
  });

  it('ignores a malformed accent colour rather than emitting broken CSS', async () => {
    const { container } = mount({
      ...BASE, features: { webapp_settings: { color: 'javascript:alert(1)' } },
    } as unknown as Agent);
    await screen.findByText('Fallback Agent');
    const shell = container.querySelector('.runner-shell') as HTMLElement;
    expect(shell.style.getPropertyValue('--teal')).toBe('');
  });
});
