// Issue-local display observations only; no HTML, remote images or graph claims.
function renderBibliography(container, data, selectSet) {
    container.replaceChildren();
    const line = (label, value, parent = container) => {
        if (value === null || value === undefined) return;
        const p = document.createElement('p');
        p.textContent = `${label}: ${value}`;
        parent.appendChild(p);
    };
    if (!data.available) {
        line('Bibliography', 'Not yet supplied by the selected provider');
        return;
    }
    line('Source', data.edition.provider);
    line('Attribution', data.selected_provider_current === false || data.selected_provider_current === 0
        ? 'Retained historical provider evidence; not the current selected authority'
        : 'Evidence attributed to the selected provider');
    if (data.variant_of) line('Variant of', `${data.variant_of.base_provider}:${data.variant_of.base_provider_id}`);
    for (const key of ['isbn', 'isbn_normalized', 'isbn_validity', 'barcode', 'page_count',
        'variant_name', 'indicia_publisher', 'indicia_printer', 'brand', 'rating', 'indicia_frequency', 'cover_reference']) {
        line(key.replaceAll('_', ' '), data.edition[key]);
    }
    for (const key of ['binding', 'publishing_format', 'color', 'dimensions', 'paper_stock']) {
        line(`Series observation — ${key.replaceAll('_', ' ')}`, data.publication?.[key]);
    }
    for (const date of data.dates || []) line(date.source_field, `${date.raw_value ?? 'unknown'} (${date.precision})`);
    line('Story scope', 'Reported issue-owned observations; active completeness is unproven. Not collected-issue contents.');
    if (data.retained_sets.length > 1) {
        const select = document.createElement('select');
        select.setAttribute('aria-label', 'Retained story observation sets');
        for (const set of data.retained_sets) {
            const option = document.createElement('option');
            option.value = String(set.id);
            option.textContent = `Local set ${set.id}: ${set.observation_count} observations`;
            option.selected = set.id === data.story_set.id;
            select.appendChild(option);
        }
        select.onchange = () => selectSet(select.value);
        container.appendChild(select);
    }
    line('Story observations', data.stories.length);
    for (const story of data.stories) {
        const section = document.createElement('section');
        container.appendChild(section);
        line('Sequence', story.sequence ?? 'unknown', section);
        for (const key of ['story_type', 'title', 'feature', 'page_count', 'characters', 'genre'])
            line(key.replaceAll('_', ' '), story[key], section);
        for (const credit of story.credits) line(credit.role, credit.text, section);
    }
    for (const diagnostic of data.diagnostics) line('Observation diagnostic', diagnostic);
    for (const diagnostic of data.publication_diagnostics || []) line('Series diagnostic', diagnostic);
}
