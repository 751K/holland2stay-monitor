'use strict';
// The snapshot contains Dutch-formatted prices. Keep the source text for display.
const InboxModel = (() => {
  const cities = ['Amsterdam', 'Delft', 'Diemen', 'Eindhoven', 'Enschede', 'Rijswijk', 'Tilburg', "'s-Hertogenbosch"];
  function euroPrice(raw) {
    const value = String(raw ?? '').replace(/[€\s\u00a0]/g, '');
    if (!/^\d+(?:\.\d{3})*(?:,\d{1,2})?$/.test(value)) return null;
    return Number(value.replace(/\./g, '').replace(',', '.'));
  }
  function cityOf(item) {
    const text = item.city || '';
    if (text.includes('Diemen')) return 'Diemen';
    return cities.find(city => text.includes(city)) || text;
  }
  function matches(item, filters) {
    if (!filters.sources.includes(item.source)) return false;
    if (filters.city && cityOf(item) !== filters.city) return false;
    const price = euroPrice(item.price_raw);
    if (filters.minPrice != null && (price == null || price < filters.minPrice)) return false;
    if (filters.maxPrice != null && (price == null || price > filters.maxPrice)) return false;
    if (filters.minArea != null && (item.area_value == null || item.area_value < filters.minArea)) return false;
    return true;
  }
  function validFilters(value, sourceIds) {
    if (!value || !Array.isArray(value.sources) || !value.sources.length || value.sources.some(id => !sourceIds.includes(id))) return false;
    if (typeof value.city !== 'string' || (value.city && !cities.includes(value.city))) return false;
    for (const key of ['minPrice', 'maxPrice', 'minArea']) {
      if (value[key] !== null && (typeof value[key] !== 'number' || !Number.isFinite(value[key]) || value[key] < 0)) return false;
    }
    return value.minPrice == null || value.maxPrice == null || value.minPrice <= value.maxPrice;
  }
  return { cities, euroPrice, cityOf, matches, validFilters };
})();
