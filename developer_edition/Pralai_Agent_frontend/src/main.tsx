import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles.css';
import './components/pralai-dark.css';
import './dark.css';
// Sets <html data-theme> at module load, before first paint (also imported by Topbar).
import './components/DeveloperUserMenu';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
