/**
 * kb-mcp Admin SPA Router & Markdown Link Autocomplete
 * Enables smooth client-side page transitions, a top progress bar,
 * and autocomplete dropdowns for document links inside the editor.
 */

// ── Progress Bar Helper ──────────────────────────────────────────────
class ProgressBar {
  constructor() {
    this.el = document.createElement('div');
    this.el.className = 'spa-progress-bar';
    document.body.appendChild(this.el);
    this.timer = null;
    this.progress = 0;
  }
  start() {
    this.progress = 10;
    this.el.style.width = '10%';
    this.el.style.opacity = '1';
    clearInterval(this.timer);
    this.timer = setInterval(() => {
      if (this.progress < 90) {
        this.progress += Math.random() * 8;
        this.el.style.width = `${this.progress}%`;
      }
    }, 200);
  }
  done() {
    clearInterval(this.timer);
    this.el.style.width = '100%';
    setTimeout(() => {
      this.el.style.opacity = '0';
      setTimeout(() => {
        this.el.style.width = '0%';
      }, 300);
    }, 100);
  }
}

// Global ProgressBar instance
window.spaProgress = new ProgressBar();

// Add progress bar styles dynamically
const style = document.createElement('style');
style.textContent = `
.spa-progress-bar {
  position: fixed;
  top: 0;
  left: 0;
  height: 3px;
  background: var(--gradient-primary, var(--primary));
  z-index: 99999;
  transition: width 0.25s ease-out, opacity 0.3s ease-in-out;
  box-shadow: 0 0 10px var(--primary);
  width: 0%;
  opacity: 0;
}
.main-transition-fade {
  opacity: 0;
  transform: translateY(6px);
  transition: opacity 0.2s ease, transform 0.2s ease;
}
.main-transition-in {
  opacity: 1;
  transform: translateY(0);
}
.wikilink-dropdown {
  position: absolute;
  z-index: 10000;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-xl);
  max-height: 220px;
  overflow-y: auto;
  min-width: 250px;
  font-family: var(--font-sans);
  font-size: 13px;
  padding: 4px;
}
.wikilink-item {
  padding: 8px 12px;
  cursor: pointer;
  border-radius: var(--radius-sm);
  display: flex;
  flex-direction: column;
  gap: 2px;
  transition: var(--transition);
  color: var(--foreground);
}
.wikilink-item.active, .wikilink-item:hover {
  background: var(--primary-soft);
  color: var(--primary);
}
.wikilink-item .wiki-title {
  font-weight: 600;
}
.wikilink-item .wiki-id {
  font-size: 11px;
  color: var(--muted-foreground);
}
`;
document.head.appendChild(style);

// ── SPA Routing Logic ────────────────────────────────────────────────
const SPA = {
  init() {
    this.mainEl = document.querySelector('main.main');
    if (!this.mainEl) return;

    this.loadedScripts = new Set(
      Array.from(document.scripts)
        .map(script => script.src)
        .filter(Boolean)
    );
    
    // Bind link clicks
    document.addEventListener('click', e => this.handleLinkClick(e));
    window.addEventListener('popstate', () => this.handlePopState());
    
    // Initial run for page-specific setup
    this.pageSpecificSetup();
  },

  async navigate(url, push = true) {
    window.spaProgress.start();
    
    // Add transitioning class
    this.mainEl.classList.add('main-transition-fade');
    this.mainEl.classList.remove('main-transition-in');
    
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error('Failed to load page');
      
      const htmlText = await response.text();
      const parser = new DOMParser();
      const doc = parser.parseFromString(htmlText, 'text/html');
      
      const newMain = doc.querySelector('main.main');
      const newTitle = doc.querySelector('title');
      
      if (!newMain) throw new Error('No main content element in fetched page');
      
      // Update Title
      if (newTitle) document.title = newTitle.textContent;
      
      // Update URL
      if (push) {
        history.pushState({ url }, '', url);
      }
      
      // Update Main Container Content
      this.mainEl.innerHTML = newMain.innerHTML;
      
      // Update Sidebar Navigation state
      const currentPath = window.location.pathname;
      document.querySelectorAll('.nav-link').forEach(link => {
        const href = link.getAttribute('href');
        link.classList.toggle('active', href === currentPath || (href !== '/' && currentPath.startsWith(href)));
      });

      // Update Breadcrumbs
      const pathMap = {
        '/': '概览',
        '/documents': '文档',
        '/types': '类型',
        '/search': '搜索',
        '/links': '链接',
        '/graph': '图谱',
        '/imports': '导入',
        '/ingest': '转换',
        '/git': 'Git',
        '/scheduler': '调度',
        '/settings': '设置'
      };
      let pageName = pathMap[currentPath];
      if (!pageName) {
        if (currentPath.startsWith('/documents/')) {
          pageName = currentPath.includes('/new') ? '新建文档' : '编辑文档';
        } else {
          pageName = '管理';
        }
      }
      const breadcrumb = document.getElementById('breadcrumb-page');
      if (breadcrumb) breadcrumb.textContent = pageName;

      // Extract and execute scripts
      const scripts = Array.from(newMain.querySelectorAll('script'));
      await this.executeScripts(scripts);
      
      // Re-trigger global triggers in the new DOM if needed
      this.pageSpecificSetup();
      
      // Complete transition
      setTimeout(() => {
        this.mainEl.classList.add('main-transition-in');
      }, 50);

    } catch (err) {
      console.error('[SPA Router Error]', err);
      // Fallback: hard reload
      window.location.href = url;
    } finally {
      window.spaProgress.done();
    }
  },

  async executeScripts(scripts) {
    for (const oldScript of scripts) {
      const source = oldScript.src;
      if (source && this.loadedScripts.has(source)) continue;

      const newScript = document.createElement('script');
      Array.from(oldScript.attributes).forEach(attr => newScript.setAttribute(attr.name, attr.value));
      newScript.textContent = oldScript.textContent;

      if (!source) {
        document.body.appendChild(newScript);
        newScript.remove();
        continue;
      }

      newScript.async = false;
      await new Promise((resolve, reject) => {
        newScript.onload = resolve;
        newScript.onerror = () => reject(new Error(`Failed to load script: ${source}`));
        document.body.appendChild(newScript);
      });
      this.loadedScripts.add(source);
      newScript.remove();
    }
  },

  handleLinkClick(e) {
    const link = e.target.closest('a');
    if (!link) return;
    
    // Ignore external links, links with target="_blank", downloads, mailto, etc.
    if (link.origin !== window.location.origin) return;
    if (link.getAttribute('target') === '_blank') return;
    if (link.hasAttribute('download')) return;
    if (link.href.includes('mailto:') || link.href.includes('tel:')) return;
    
    // Skip if it is a standard form submission or button action
    if (link.closest('.no-spa')) return;
    
    // Don't hijack links that explicitly reload (e.g. vault switch etc.)
    e.preventDefault();
    this.navigate(link.href);
  },

  handlePopState() {
    this.navigate(window.location.pathname + window.location.search, false);
  },

  pageSpecificSetup() {
    this.mainEl.classList.remove('main-transition-fade');
    this.mainEl.classList.add('main-transition-in');
    
    // If we have SimpleMDE, initialize our autolinks autocomplete on it
    setTimeout(() => {
      this.setupEditorAutocomplete();
    }, 100);
  },

  // ── CodeMirror [[WikiLink]] / Markdown Autocomplete ────────────────
  setupEditorAutocomplete() {
    const textarea = document.getElementById('markdown-editor');
    if (!textarea) return;
    
    // Find the SimpleMDE instance
    // Wait until SimpleMDE script initializes on CodeMirror
    let codemirrorInstance = null;
    const parentForm = textarea.closest('form');
    
    if (parentForm) {
      // Find initialized SimpleMDE instance or codemirror wrapper
      const cmEl = parentForm.querySelector('.CodeMirror');
      if (cmEl && cmEl.CodeMirror) {
        codemirrorInstance = cmEl.CodeMirror;
      }
    }
    
    if (!codemirrorInstance) {
      // Retry in 100ms
      setTimeout(() => this.setupEditorAutocomplete(), 100);
      return;
    }

    const cm = codemirrorInstance;
    let dropdown = null;
    let activeIndex = 0;
    let itemsList = [];
    let queryStartPos = null;

    function getCursorCoords() {
      const cursor = cm.getCursor();
      const coords = cm.cursorCoords(cursor);
      return coords;
    }

    function createDropdown() {
      destroyDropdown();
      dropdown = document.createElement('div');
      dropdown.className = 'wikilink-dropdown';
      document.body.appendChild(dropdown);
    }

    function destroyDropdown() {
      if (dropdown) {
        dropdown.remove();
        dropdown = null;
      }
    }

    async function fetchDocs(q) {
      try {
        const resp = await fetch(`/api/docs?q=${encodeURIComponent(q)}&include_deleted=false`);
        if (!resp.ok) return [];
        const data = await resp.json();
        return data.items || [];
      } catch (e) {
        return [];
      }
    }

    async function handleTextChange(instance, changeObj) {
      const cursor = cm.getCursor();
      const lineText = cm.getLine(cursor.line);
      const beforeCursor = lineText.slice(0, cursor.ch);
      
      // Look for [[ or [
      // If we find `[[` followed by chars, activate query
      const match = beforeCursor.match(/\[\[([^[\]]*)$/);
      if (match) {
        const query = match[1];
        const index = beforeCursor.lastIndexOf('[[');
        queryStartPos = { line: cursor.line, ch: index };
        
        if (!dropdown) {
          createDropdown();
        }
        
        // Fetch matching documents
        itemsList = await fetchDocs(query);
        renderDropdown();
      } else {
        destroyDropdown();
      }
    }

    function renderDropdown() {
      if (!dropdown) return;
      if (itemsList.length === 0) {
        dropdown.innerHTML = '<div style="padding:8px; color:var(--muted-foreground)">No documents found</div>';
        return;
      }

      dropdown.innerHTML = itemsList.map((doc, idx) => `
        <div class="wikilink-item ${idx === activeIndex ? 'active' : ''}" data-idx="${idx}">
          <span class="wiki-title">${doc.title}</span>
          <span class="wiki-id">${doc.id}</span>
        </div>
      `).join('');

      // Position the dropdown near the cursor
      const coords = getCursorCoords();
      dropdown.style.left = `${coords.left}px`;
      dropdown.style.top = `${coords.bottom + 5}px`;

      // Scroll active item into view
      const activeEl = dropdown.querySelector('.wikilink-item.active');
      if (activeEl) {
        activeEl.scrollIntoView({ block: 'nearest' });
      }

      // Bind click on items
      dropdown.querySelectorAll('.wikilink-item').forEach(el => {
        el.addEventListener('click', () => {
          selectItem(parseInt(el.getAttribute('data-idx'), 10));
        });
      });
    }

    function selectItem(idx) {
      const doc = itemsList[idx];
      if (!doc || !queryStartPos) return;

      const cursor = cm.getCursor();
      // Replace from queryStartPos to current cursor with standard markdown link
      // Format: [Title](/documents/id)
      const linkText = `[${doc.title}](/documents/${doc.id})`;
      cm.replaceRange(linkText, queryStartPos, cursor);
      destroyDropdown();
      cm.focus();
    }

    // Intercept keyboard controls when dropdown is active
    cm.on('keydown', (instance, event) => {
      if (!dropdown || itemsList.length === 0) return;

      if (event.key === 'ArrowDown') {
        event.preventDefault();
        activeIndex = (activeIndex + 1) % itemsList.length;
        renderDropdown();
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        activeIndex = (activeIndex - 1 + itemsList.length) % itemsList.length;
        renderDropdown();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        selectItem(activeIndex);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        destroyDropdown();
      }
    });

    cm.on('change', handleTextChange);
    cm.on('blur', () => {
      // Small delay to allow clicking on dropdown items
      setTimeout(destroyDropdown, 200);
    });
  }
};

// Initialize SPA
window.SPA = SPA;
document.addEventListener('DOMContentLoaded', () => SPA.init());
