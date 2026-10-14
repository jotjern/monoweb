import { createPrisma } from "../../packages/db/src/index"

const databaseUrl = process.env.DATABASE_URL
const previewId = process.env.PREVIEW_ID
const adminId = process.env.PREVIEW_ADMIN_ID
if (!databaseUrl || !previewId || !/^(main|poc|pr-[1-9][0-9]*)$/.test(previewId) || !adminId) {
  throw new Error("Preview seed requires DATABASE_URL, PREVIEW_ID, and PREVIEW_ADMIN_ID")
}
const db = createPrisma(databaseUrl)
try {
  await db.user.upsert({
    where: { id: adminId },
    update: {},
    create: {
      id: adminId,
      username: `preview-admin-${previewId}`,
      name: "Preview Administrator",
      email: `admin.${previewId}@preview.example.com`,
    },
  })
  await db.group.upsert({
    where: { slug: "dotkom" },
    update: {},
    create: {
      slug: "dotkom",
      abbreviation: "Dotkom",
      name: "Preview committee",
      description: "Synthetic preview committee",
      type: "COMMITTEE",
    },
  })
  const role = await db.groupRole.upsert({
    where: { groupId_name: { groupId: "dotkom", name: "Preview editor" } },
    update: {},
    create: { groupId: "dotkom", name: "Preview editor", type: "COSMETIC" },
  })
  const membershipId = `preview-${previewId}-admin-membership`
  await db.groupMembership.upsert({
    where: { id: membershipId },
    update: {},
    create: { id: membershipId, groupId: "dotkom", userId: adminId, start: new Date("2026-01-01") },
  })
  await db.groupMembershipRole.upsert({
    where: { membershipId_roleId: { membershipId, roleId: role.id } },
    update: {},
    create: { membershipId, roleId: role.id },
  })
  await db.article.upsert({
    where: { slug: "preview-welcome" },
    update: { imageUrl: `${process.env.PREVIEW_ORIGIN}/online-logo.svg` },
    create: {
      slug: "preview-welcome",
      title: `Welcome to the ${previewId} preview`,
      author: "Preview team",
      imageUrl: `${process.env.PREVIEW_ORIGIN}/online-logo.svg`,
      excerpt: "Synthetic data in an isolated PlanetScale branch.",
      content: "# Branch preview\n\nThis content is synthetic. Changes here affect only this preview.",
      isFeatured: true,
    },
  })
  await db.event.upsert({
    where: { id: "6e2cae72-3e9f-4aab-952a-fb24e37da001" },
    update: {},
    create: {
      id: "6e2cae72-3e9f-4aab-952a-fb24e37da001",
      title: `Synthetic event (${previewId})`,
      start: new Date(Date.now() + 7 * 86400000),
      end: new Date(Date.now() + 7 * 86400000 + 7200000),
      status: "PUBLIC",
      type: "SOCIAL",
      description: "A test event for this preview only.",
      hostingGroups: { create: { groupId: "dotkom" } },
    },
  })
  console.log(`Synthetic seed ready for ${previewId}`)
} finally {
  await db.$disconnect()
}
