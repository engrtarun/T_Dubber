# Components & Design System

# Design System Overview  

---

## 1. Design Tokens  

### 1.1 Color Palette  

| Purpose                     | Color Value | Description |
|-----------------------------|------------|-------------|
| **Primary Brand**           | `#0078D4`  | Microsoft “Azure” blue – used for main actions, links, and brand accents. |
| **Primary Hover/Active**    | `#005A9E`  | Darker shade of the primary brand for hover and active states. |
| **Secondary Accent**        | `#00A4EF`  | Light blue used for secondary buttons and informational highlights. |
| **Success**                 | `#107C10`  | Green for success messages, check‑marks, and positive feedback. |
| **Warning**                 | `#FFB900`  | Yellow‑orange for warnings, alerts, and attention‑drawing UI. |
| **Error**                   | `#D13438`  | Red for error states, validation messages, and destructive actions. |
| **Background – Light**      | `#FFFFFF`  | Main page background. |
| **Background – Dark**       | `#F3F2F1`  | Secondary background for cards, panels, and sidebars. |
| **Surface – Card**          | `#FAFAFA`  | Card and modal surface background. |
| **Text – Primary**          | `#212121`  | Main body copy. |
| **Text – Secondary**        | `#5A5A5A`  | Sub‑text, captions, and disabled text. |
| **Border / Divider**        | `#E1E1E1`  | Light borders and dividers. |
| **Overlay / Scrim**         | `rgba(0,0,0,0.4)` | Modal overlay. |

### 1.2 Typography  

| Token | Value |
|-------|-------|
| **Font Family – Primary** | `Segoe UI, system-ui, -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif` |
| **Font Family – Monospace** | `Consolas, "Courier New", monospace` |
| **Font Weight – Regular** | `400` |
| **Font Weight – Medium** | `500` |
| **Font Weight – Bold** | `600` |

### 1.3 Font‑Size Scale  

| Scale | Pixel | Rem | Usage |
|-------|-------|-----|-------|
| **Display** | 48 px | 3rem | Large hero headings (e.g., Home page “Get started”). |
| **H1** | 32 px | 2rem | Primary page titles (`h1`). |
| **H2** | 24 px | 1.5rem | Section headings (`h2`). |
| **H3** | 20 px | 1.25rem | Sub‑section headings (`h3`). |
| **Body Large** | 16 px | 1rem | Main copy, button labels. |
| **Body Small** | 14 px | 0.875rem | Secondary copy, captions. |
| **Micro** | 12 px | 0.75rem | Helper text, timestamps. |

### 1.4 Border Radius  

| Token | Value | Applied To |
|-------|-------|------------|
| **Radius – Small** | `2px` | Input fields, small buttons. |
| **Radius – Medium** | `4px` | Cards, list items, secondary buttons. |
| **Radius – Large** | `8px` | Modals, primary CTA buttons. |

---

## 2. Shared Component Inventory  

| Component | Appears On Pages | Purpose / Description |
|-----------|------------------|-----------------------|
| **Global Header / Top Nav** (`<header class="globalHeader">`) | All pages (Home, My Files, Shared, Recycle Bin, People, Storage Settings) | Provides brand logo, primary navigation links (Home, My files, Shared, Recycle bin, People, Storage), user avatar, and quick‑action icons. |
| **Side Navigation (collapsed on mobile)** (`<nav class="sideNav">`) | Home, My Files, Shared, Recycle Bin, People, Storage Settings | Secondary navigation for deeper sections (e.g., Manage storage, Settings). |
| **NavLink List Item** (`li.navLink_ed0dcd6a`) | All pages (repeated in side nav) | Individual navigation link with active state styling. |
| **Primary Button** (`button.primaryBtn`) | All pages (e.g., “Get more storage”, “Create or upload”) | Calls‑to‑action with primary brand color. |
| **Secondary Button** (`button.secondaryBtn`) | All pages (e.g., “Browse files by”) | Less‑prominent actions, often outlined. |
| **Card Wrapper** (`li.cardWrapper_f90905be`) | Home (featured apps), My Files (file cards), Shared, Recycle Bin | Container for a file/app preview, title, and meta information. |
| **File Row** (`div.row_594fd7b1`) | My Files, Shared, Recycle Bin | Horizontal list item showing file icon, name, date, owner, size, and privacy badge. |
| **Table Header Cell** (`div.odsp-spartan-headerCell`) | Recycle Bin (list view) | Column header for file tables (Name, Modified, Size, etc.). |
| **Table Body Cell** (`div.odsp-spartan-cell`) | Recycle Bin (list view) | Individual cell content aligned under header cells. |
| **Footer** (`<footer class="globalFooter">`) | All pages (implicit) | Contains legal links, language selector, and Microsoft copyright. |
| **Search Bar** (`input.searchInput`) | Global Header (all pages) | Allows quick file/folder search. |
| **Breadcrumb** (`nav.breadcrumb`) | My Files, Shared, Recycle Bin, Storage Settings | Shows current location hierarchy and enables quick navigation up the tree. |
| **Empty State Illustration** (`div.emptyState`) | Pages where no content exists (e.g., empty “Shared” view) | Communicates lack of items with illustration and CTA. |
| **Pagination Controls** (`div.pagination`) | Recycle Bin (large lists) | Navigate through multiple pages of items. |
| **Modal Dialog** (`div.modal`) | Storage Settings (upgrade prompts), Delete confirmations | Focused interaction overlay. |
| **Toast / Snackbar** (`div.toast`) | Across the site (e.g., “File uploaded”) | Transient feedback messages. |

---

## 3. Layout Patterns  

| Pattern | Description | Where Used |
|---------|-------------|------------|
| **Top‑Bar + Side‑Nav Layout** | A fixed global header spans the full width. A collapsible vertical side navigation sits beneath the header on the left, providing primary navigation links. Main content occupies the remaining horizontal space. | All main pages (Home, My Files, Shared, Recycle Bin, People, Storage Settings). |
| **Responsive Grid of Cards** | Content area uses a responsive CSS grid (`grid-template-columns: repeat(auto-fill, minmax(240px, 1fr))`). Cards (`cardWrapper_f90905be`) automatically wrap based on viewport width. | Home (featured apps), My Files (preview cards). |
| **List / Table Flow** | For file‑heavy pages (My Files, Shared, Recycle Bin) a vertical list or table layout is used. Each row (`row_594fd7b1`) contains an icon, title, metadata, and action menu. On wider screens the list switches to a table with header cells (`odsp-spartan-headerCell`). | My Files, Shared, Recycle Bin. |
| **Info‑Flow (Detail Pane)** | When a file/folder is selected, a right‑hand pane slides in, showing details, preview, and actions. This pattern is optional and appears on larger screens. | Recycle Bin (preview of selected backup), Storage Settings (plan details). |
| **Modal Overlay** | Full‑screen or centered modal dialogs for actions like “Upgrade storage”, “Delete confirmation”, or “Create folder”. Uses the overlay token (`rgba(0,0,0,0.4)`). | Storage Settings, various CTA flows. |
| **Empty State Centered Layout** | When a section has no items, a centered illustration with a headline, supporting copy, and a primary CTA is displayed. | Shared (no shared items), Recycle Bin (empty). |
| **Footer Stacked Layout** | Footer content stacks vertically on mobile and aligns horizontally on desktop. Contains legal links, language selector, and brand info. | All pages (global). |

**Responsive Behavior**  

* **Desktop (≥ 1024 px)** – Side navigation visible, grid shows 4‑column card layout, table view for lists.  
* **Tablet (768 px – 1023 px)** – Side navigation collapses to icons only, grid reduces to 2‑3 columns, list remains.  
* **Mobile (< 768 px)** – Side navigation hidden behind a hamburger menu, cards become single‑column, list switches to stacked cards, header actions collapse into an overflow menu.  

---  

*This document captures the core visual language, reusable UI building blocks, and layout strategies observed on the OneDrive web experience, forming a baseline design system that can be extended for future features and platforms.*
