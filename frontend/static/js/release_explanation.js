/* Read-only reusable renderer, shared by evaluated manual search results.
 * Input must be an actual backend explanation DTO, never a legacy match flag.
 */
function renderReleaseExplanation(container, dto) {
    const states = ['compatible', 'review_required', 'undetermined', 'rejected'];
    if (!dto || dto.explanation_policy !== 'kapowarr-release-explanation/v1'
        || !states.includes(dto.state) || !Array.isArray(dto.entries)) {
        throw new Error('Release explanation unavailable');
    }
    const doc = container.ownerDocument;
    const root = doc.createElement('section');
    root.setAttribute('aria-label', 'Release evaluation explanation');
    const append = (parent, tag, text) => {
        const node = doc.createElement(tag);
        node.textContent = text;
        parent.appendChild(node);
        return node;
    };
    append(root, 'h3', dto.headline);
    append(root, 'p', dto.candidate.raw_title);
    const source = dto.candidate.source;
    append(root, 'p', `Source: ${source.name}${source.via ? ' via ' + source.via : ''}`);
    if (dto.candidate.mechanism) {
        const labels = {nzb: 'Usenet', torrent: 'Torrent', direct_download: 'DDL'};
        append(root, 'p', `Protocol: ${labels[dto.candidate.mechanism] || dto.candidate.mechanism}`);
    }
    if (dto.candidate.size_bytes != null) append(root, 'p', `Reported size: ${dto.candidate.size_bytes} bytes`);
    const torrent = dto.candidate.torrent_facts || {};
    if (torrent.seeders != null) append(root, 'p', `Seeders: ${torrent.seeders} (source observation, not comic quality)`);
    if (torrent.downloadvolumefactor != null)
        append(root, 'p', `Download accounting factor: ${torrent.downloadvolumefactor} (source policy, not quality)`);
    append(root, 'p', `Coverage: ${dto.coverage}`);
    if (dto.score !== null) append(root, 'p', `Score: ${dto.score} policy points`);
    const labels = {positive: 'Contribution', penalty: 'Penalty', rejection: 'Rejection',
        review: 'Review', undetermined: 'Insufficient evidence', neutral: 'Information', tie_break: 'Tie-break fact'};
    const entry = (parent, value, detailed) => {
        const node = doc.createElement('li');
        const points = dto.score !== null && value.points !== 0
            ? `${value.points > 0 ? '+' : ''}${value.points} ` : '';
        node.textContent = `${labels[value.kind] || 'Information'}: ${points}${value.message}`;
        parent.appendChild(node);
        if (detailed) {
            for (const evidence of value.evidence) {
                append(node, 'p', `${evidence.label}: ${evidence.values.join(', ')}`);
            }
        }
    };
    const concise = append(root, 'ul', '');
    for (const index of dto.concise_entries) entry(concise, dto.entries[index], false);
    const details = doc.createElement('details');
    root.appendChild(details);
    append(details, 'summary', 'Scoring details and evidence');
    const list = append(details, 'ol', '');
    for (const value of dto.entries) entry(list, value, true);
    append(details, 'p', `Target: ${dto.target.series}; issue labels: ${dto.target.issues.map(i => i.label).join(', ')}`);
    append(details, 'p', `Target series year: ${dto.target.series_year ?? 'unknown'}; authority: ${dto.target.authority}`);
    for (const observation of dto.candidate.observations) {
        append(details, 'p', `${observation.origin}: observed ${observation.coverage.kind} coverage; labels: ${observation.coverage.labels.join(', ')}; pack: ${observation.pack}`);
    }
    append(details, 'p', `Scoring policy: ${dto.scoring_policy}`);
    append(details, 'p', `Explanation policy: ${dto.explanation_policy}`);
    append(details, 'p', 'Compatibility and ranking do not authorize downloading.');
    container.replaceChildren(root);
    return root;
}
